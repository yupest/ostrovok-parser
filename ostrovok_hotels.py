from playwright.sync_api import sync_playwright
import time
import json
import math
import sys
import os
import csv
import logging
from pathlib import Path
from datetime import date, timedelta, datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from card_status import STATUS_FIELDS, DAILY_FIELDS, CardChecker, reconcile_catalog
from log_config import setup_logging, get_log_file_path, send_telegram_summary

# Настройка stdout для корректного вывода Юникода и сброс буфера в CI
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
sys.stdout.reconfigure(line_buffering=True)

logger = logging.getLogger(__name__)


def _is_ci():
    return os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("CI") == "true"


class OstrovokHotelsDailyParser:
    def __init__(self):
        self.base_url = "https://ostrovok.ru/hotel/russia/western_siberia_irkutsk_oblast_multi/"
        self.api_endpoint = "/hotel/search/v2/site/serp"
        self.region_id = "965821539"  # ID региона для Иркутской области
        self.all_hotels = []
        self.current_dir = Path(__file__).parent
        self.ci = _is_ci()
        self.page_timeout_ms = int(os.environ.get("SEARCH_PAGE_TIMEOUT_MS", "90000"))
        self.page_retries = int(os.environ.get("SEARCH_PAGE_RETRIES", "3"))
        self.max_pages = int(os.environ.get("SEARCH_MAX_PAGES", "500"))
        self._attempt = None
        self._request_attempts = {}
        self.page_diagnostics = []
        if self.ci:
            logger.info("Режим CI: увеличенные таймауты и ожидание networkidle.")
    
    def _run_date(self):
        """Дата запуска по RUN_TZ (по умолчанию Asia/Irkutsk)."""
        tz_name = os.environ.get("RUN_TZ", "Asia/Irkutsk")
        try:
            return datetime.now(ZoneInfo(tz_name)).date()
        except Exception:
            return date.today()
    
    def get_all_hotels_list(self):
        """Основная функция для парсинга списка отелей на следующие 2 дня"""
        logger.info("Запуск парсера отелей...")
        today = self._run_date()
        arrival_date = today + timedelta(days=1)
        departure_date = today + timedelta(days=2)
        search_url = self._build_search_url(arrival_date, departure_date)
        
        logger.info("Даты бронирования: %s - %s", arrival_date.strftime('%d.%m.%Y'), departure_date.strftime('%d.%m.%Y'))
        
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale='ru-RU',
                viewport={"width": 1920, "height": 1080}
            )
            page = context.new_page()
            
            self._setup_response_interceptor(page)
            self._parse_all_pages_with_pagination(page, search_url)
            browser.close()
        
        if self.all_hotels:
            self._deduplicate_hotels()
            self._save_to_csv()
            logger.info("Парсинг завершён. Всего обработано %s отелей.", len(self.all_hotels))
        else:
            raise RuntimeError("Completed search returned no hotels; refusing to run rooms parser on an old daily file")
        
        return self.all_hotels
            
    def _build_search_url(self, arrival_date, departure_date):
        """Построение URL поиска для CSV столбца show_rooms_url"""
        dates_str = f"{arrival_date.strftime('%d.%m.%Y')}-{departure_date.strftime('%d.%m.%Y')}"
        url = (
            f"{self.base_url}"
            f"?search=yes"
            f"&dates={dates_str}"
            f"&guests=1"
            f"&q={self.region_id}"
        )
        return url
    
    def _new_attempt(self):
        return {"hotels": {}, "finished": False, "total": None, "errors": [], "responses": 0}

    def _consume_search_response(self, state, status, payload):
        """Accumulate polling batches; an empty unfinished response is not EOF."""
        state["responses"] += 1
        if status != 200:
            state["errors"].append(f"HTTP {status}")
            return
        if not isinstance(payload, dict) or "hotels" not in payload or (payload["hotels"] is not None and not isinstance(payload["hotels"], list)):
            state["errors"].append("Invalid response: hotels must be a list")
            return
        if payload.get("error") or payload.get("err"):
            state["errors"].append("API error in HTTP 200 response")
            return
        extracted = self._extract_hotels_from_json(payload)
        if len(extracted) != len(payload["hotels"] or []):
            state["errors"].append("Some hotel records could not be parsed")
            return
        for hotel in extracted:
            state["hotels"][(hotel["ota_hotel_id"], hotel["master_id"])] = hotel
        if payload.get("search_finished") is True:
            total = payload.get("total_av_hotels")
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                state["errors"].append("Missing/invalid total_av_hotels in completed search")
                return
            state["total"] = total
            state["finished"] = True

    def _setup_response_interceptor(self, page):
        def is_search(request):
            return request.method == "POST" and self.api_endpoint in request.url

        def handle_request(request):
            if is_search(request) and self._attempt is not None:
                self._request_attempts[request] = self._attempt

        def handle_response(response):
            if not is_search(response.request):
                return
            state = self._request_attempts.pop(response.request, None)
            # Requests from previous navigations cannot complete the current page.
            if state is None or state is not self._attempt:
                return
            try:
                payload = response.json() if response.status == 200 else None
                self._consume_search_response(state, response.status, payload)
            except Exception as exc:
                state["errors"].append(f"Response decode failed: {type(exc).__name__}")

        def handle_failure(request):
            state = self._request_attempts.pop(request, None)
            if state is self._attempt and state is not None:
                state["errors"].append("Search request failed")

        page.on("request", handle_request)
        page.on("response", handle_response)
        page.on("requestfailed", handle_failure)

    def _fetch_search_page(self, page, page_url):
        state = self._new_attempt()
        self._attempt = state
        # Old requests are ignored, and do not accumulate over a long run.
        self._request_attempts.clear()
        try:
            response = page.goto(page_url, wait_until="domcontentloaded", timeout=self.page_timeout_ms)
            if response is not None and response.status >= 400:
                state["errors"].append(f"Document HTTP {response.status}")
            deadline = time.monotonic() + self.page_timeout_ms / 1000
            while not state["finished"] and time.monotonic() < deadline:
                # Playwright must pump events; time.sleep prevents response callbacks.
                page.wait_for_timeout(250)
            if not state["finished"]:
                state["errors"].append("Timed out waiting for search_finished=true")
        except Exception as exc:
            state["errors"].append(f"Page load failed: {type(exc).__name__}")
        finally:
            self._attempt = None
        return state

    def _write_search_diagnostics(self):
        directory = self.current_dir / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self._run_date().isoformat()}-search.json"
        path.write_text(json.dumps(self.page_diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")

    def _parse_all_pages_with_pagination(self, page, base_search_url):
        """Only a completed, reconciled API response ends pagination successfully."""
        self.all_hotels = []
        self.page_diagnostics = []
        seen = set()
        max_page_size = 0
        previous_page_size = 0
        for current_page in range(1, self.max_pages + 1):
            page_url = base_search_url if current_page == 1 else self._add_page_to_url(base_search_url, current_page)
            confirmed_empty = 0
            for attempt in range(1, self.page_retries + 1):
                logger.info("Страница %s, попытка %s/%s", current_page, attempt, self.page_retries)
                state = self._fetch_search_page(page, page_url)
                new = {key: value for key, value in state["hotels"].items() if key not in seen}
                errors = list(state["errors"])
                # The search is live: availability and ranking can change between pages.
                # Allow only a small, explicitly logged count drift after a short last
                # page and repeated clean empty responses beyond the advertised last page.
                total = state["total"]
                gap = max(0, total - len(seen)) if total is not None else 0
                drift_limit = max(2, min(20, math.ceil((total or 0) * 0.01)))
                terminal_candidate = (
                    not errors and state["finished"] and total is not None and total > 0
                    and not state["hotels"] and max_page_size > 0
                    and 0 < previous_page_size < max_page_size
                    and (current_page - 1) * max_page_size >= total
                    and 0 < gap <= drift_limit
                )
                confirmed_empty = confirmed_empty + 1 if terminal_candidate else 0
                confirmed_end = confirmed_empty >= max(2, self.page_retries)
                if not state["finished"]:
                    errors.append("Search not completed")
                elif total == 0 and (seen or state["hotels"]):
                    errors.append("Zero total contradicts already received hotels")
                elif not new and total != 0 and not confirmed_end:
                    errors.append("Confirming empty last page" if terminal_candidate else "No new hotels before reported total reached")
                diagnostic = {"page": current_page, "attempt": attempt,
                              "search_finished": state["finished"], "total_av_hotels": state["total"],
                              "page_unique": len(state["hotels"]), "new_unique": len(new),
                              "collected_before": len(seen), "responses": state["responses"], "errors": errors,
                              "confirmed_empty": confirmed_empty, "count_gap": gap,
                              "outcome": "completed_with_count_drift" if confirmed_end else "page"}
                self.page_diagnostics.append(diagnostic)
                self._write_search_diagnostics()
                if confirmed_end:
                    logger.warning("Конец выдачи подтверждён %s завершёнными пустыми ответами. "
                                   "Собрано %s, API сообщает %s; расхождение %s. "
                                   "Сохраняем фактически найденные объекты без добавления пропущенных.",
                                   confirmed_empty, len(seen), total, gap)
                    return
                if not errors:
                    previous_page_size = len(state["hotels"])
                    max_page_size = max(max_page_size, previous_page_size)
                    self.all_hotels.extend(new.values())
                    seen.update(new)
                    logger.info("Страница %s: новых %s, всего %s / %s", current_page, len(new), len(seen), state["total"])
                    if len(seen) >= state["total"]:
                        logger.info("Поиск завершён: собрано %s уникальных объектов, API сообщает %s", len(seen), state["total"])
                        return
                    break
                logger.warning("Страница %s не подтверждена: %s", current_page, "; ".join(errors))
                if attempt == self.page_retries:
                    raise RuntimeError(f"Page {current_page} failed after {attempt} attempts; partial data will not be saved")
                page.wait_for_timeout(2000 * attempt)
        raise RuntimeError(f"SEARCH_MAX_PAGES={self.max_pages} reached before all hotels were collected")

    def _add_page_to_url(self, url, page_number):
        """Добавление номера страницы к URL"""
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        qs["page"] = [str(page_number)]
        new_query = urlencode(qs, doseq=True)
        return urlunparse(parsed._replace(query=new_query))
    
    def _extract_hotels_from_json(self, json_data):
        """Извлечение отелей из JSON ответа от API Ostrovok"""
        hotels_list = []
        
        if not json_data or "hotels" not in json_data:
            return hotels_list
        
        hotels_array = json_data["hotels"]
        
        if hotels_array is None:
            return hotels_list
        
        if not isinstance(hotels_array, list):
            if isinstance(hotels_array, dict):
                hotels_array = [hotels_array]
            else:
                return hotels_list
        
        for hotel in hotels_array:
            try:
                static_vm = hotel.get("static_vm", {})
                if not static_vm:
                    continue
                
                ota_hotel_id = hotel.get("ota_hotel_id", "")
                name = static_vm.get("name", "")
                
                if not ota_hotel_id or not name:
                    continue
                
                master_id = str(hotel.get("master_id") or static_vm.get("master_id", ""))
                url = f"https://ostrovok.ru/hotel/russia/irkutsk/mid{master_id}/{ota_hotel_id}"
                
                hotel_data = {
                    "city": static_vm.get("city", ""),
                    "ota_hotel_id": ota_hotel_id,
                    "master_id": master_id,
                    "name": name,
                    "name_en": static_vm.get("name_en", ""),
                    "address": static_vm.get("address", ""),
                    "latitude": str(static_vm.get("latitude", "")),
                    "longitude": str(static_vm.get("longitude", "")),
                    "url": url,
                    "rooms_number": str(static_vm.get("rooms_number", "")),
                }
                
                hotels_list.append(hotel_data)
            except Exception as e:
                continue
        
        return hotels_list
    
    def _deduplicate_hotels(self):
        """Удаление дубликатов по (ota_hotel_id, master_id), порядок сохраняется."""
        seen = set()
        unique = []
        for h in self.all_hotels:
            key = (h.get("ota_hotel_id") or "", h.get("master_id") or "")
            if key not in seen:
                seen.add(key)
                unique.append(h)
        removed = len(self.all_hotels) - len(unique)
        if removed:
            logger.info("Убрано дубликатов: %s. Уникальных отелей: %s", removed, len(unique))
        self.all_hotels = unique
    
    def _save_to_csv(self):
        """Сохранение списка отелей в CSV файл (daily/hotels/YYYY-MM-DD.csv)"""
        if not self.all_hotels:
            return
        
        run_date = self._run_date()
        output_dir = self.current_dir / 'daily' / 'hotels'
        output_dir.mkdir(parents=True, exist_ok=True)
        csv_filename = output_dir / f'{run_date.isoformat()}.csv'
        
        fieldnames = [
            'city',
            'ota_hotel_id',
            'master_id',
            'name',
            'name_en',
            'address',
            'latitude',
            'longitude',
            'url',
            'rooms_number'
        ] + DAILY_FIELDS
        
        try:
            with open(csv_filename, 'w', encoding='utf-8-sig', newline='') as csv_file:
                writer = csv.DictWriter(csv_file, fieldnames=fieldnames, extrasaction="ignore", delimiter=',', quoting=csv.QUOTE_MINIMAL)
                writer.writeheader()
                for hotel in self.all_hotels:
                    writer.writerow(hotel)
            logger.info("Сохранено %s отелей в %s", len(self.all_hotels), csv_filename)
        except Exception as e:
            logger.error("Ошибка при сохранении CSV: %s", e)
            raise


class OstrovokHotelsCatalog:
    """Ведёт накопленный каталог спарсенных отелей за всё время.
    При каждом запуске — добавляет новые отели и обновляет last_seen_date у существующих.
    Файл: catalog/hotels.csv"""

    FIELDNAMES = [
        'ota_hotel_id', 'master_id', 'name', 'name_en',
        'city', 'address', 'latitude', 'longitude', 'url', 'rooms_number',
        'first_seen_date', 'last_seen_date',
    ] + STATUS_FIELDS

    def __init__(self):
        self.current_dir = Path(__file__).parent
        self.catalog_path = self.current_dir / 'catalog' / 'hotels.csv'

    def _run_date(self):
        tz_name = os.environ.get("RUN_TZ", "Asia/Irkutsk")
        try:
            return datetime.now(ZoneInfo(tz_name)).date()
        except Exception:
            return date.today()

    def _load_existing(self):
        """Читает текущий каталог."""
        existing = {}
        if not self.catalog_path.exists():
            return existing
        try:
            with open(self.catalog_path, 'r', encoding='utf-8-sig', newline='') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    hotel_id = row.get('ota_hotel_id', '')
                    if hotel_id:
                        existing[hotel_id] = row
        except Exception as e:
            logger.error("Ошибка при чтении каталога %s: %s", self.catalog_path, e)
            raise
        return existing

    def _save(self, hotels: dict):
        """Сохраняет каталог."""
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.catalog_path, 'w', encoding='utf-8-sig', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=self.FIELDNAMES, quoting=csv.QUOTE_MINIMAL)
                writer.writeheader()
                writer.writerows(hotels.values())
            logger.info("Каталог сохранён: %s отелей → %s", len(hotels), self.catalog_path)
        except Exception as e:
            logger.error("Ошибка при сохранении каталога: %s", e)
            raise

    def update(self, parsed_hotels: list):
        """Обновляет каталог на основе последнего списка отелей.
        - Новые отели добавляются с first_seen_date = сегодня.
        - Существующие — обновляют поля и last_seen_date."""
        today = self._run_date().isoformat()
        existing = self._load_existing()

        new_count = 0
        for hotel in parsed_hotels:
            hotel_id = hotel.get('ota_hotel_id', '')
            if not hotel_id:
                continue

            # Обновляем поля и дату последнего появления
            if hotel_id in existing:
                existing[hotel_id].update({
                    'name':         hotel.get('name', existing[hotel_id]['name']),
                    'name_en':      hotel.get('name_en', existing[hotel_id]['name_en']),
                    'city':         hotel.get('city', existing[hotel_id]['city']),
                    'address':      hotel.get('address', existing[hotel_id]['address']),
                    'latitude':     hotel.get('latitude', existing[hotel_id].get('latitude', '')),
                    'longitude':    hotel.get('longitude', existing[hotel_id].get('longitude', '')),
                    'url':          hotel.get('url', existing[hotel_id]['url']),
                    'rooms_number': hotel.get('rooms_number', existing[hotel_id]['rooms_number']),
                    'last_seen_date': today,
                })
            # Новый отель
            else:
                existing[hotel_id] = {
                    'ota_hotel_id':   hotel_id,
                    'master_id':      hotel.get('master_id', ''),
                    'name':           hotel.get('name', ''),
                    'name_en':        hotel.get('name_en', ''),
                    'city':           hotel.get('city', ''),
                    'address':        hotel.get('address', ''),
                    'latitude':       hotel.get('latitude', ''),
                    'longitude':      hotel.get('longitude', ''),
                    'url':            hotel.get('url', ''),
                    'rooms_number':   hotel.get('rooms_number', ''),
                    'first_seen_date': today,
                    'last_seen_date':  today,
                }
                new_count += 1

        self._save(existing)
        logger.info("Каталог обновлён: всего %s, новых %s", len(existing), new_count)
        return len(existing), new_count


def _run_date_for_log():
    tz_name = os.environ.get("RUN_TZ", "Asia/Irkutsk")
    try:
        return datetime.now(ZoneInfo(tz_name)).date()
    except Exception:
        return date.today()


if __name__ == "__main__":
    run_date = _run_date_for_log()
    setup_logging(log_file=get_log_file_path(run_date))

    parser = OstrovokHotelsDailyParser()
    result = parser.get_all_hotels_list()
    
    catalog = OstrovokHotelsCatalog()
    total, new_count = catalog.update(result)
    existing = catalog._load_existing()
    parser.all_hotels = reconcile_catalog(existing, result, CardChecker(), run_date.isoformat())
    catalog._save(existing)
    parser._save_to_csv()
    logger.info("Дневной список после проверки каталога: %s объектов", len(parser.all_hotels))


    send_telegram_summary(
        f"Ostrovok: парсинг отелей завершён. Отелей: {len(result)}."
        f"Каталог: {total} всего, {new_count} новых. Дата: {run_date}."
    )