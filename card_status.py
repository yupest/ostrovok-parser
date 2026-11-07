"""Check missing catalog cards separately from daily search availability."""
import logging
import json
from accommodation_types import is_apartment
from datetime import date,timedelta
from future_availability import CONTROL_FIELDS, ASSUMED_FULL, FutureAvailabilityChecker
import re
from html.parser import HTMLParser
from urllib.parse import urlparse
import requests

logger = logging.getLogger(__name__)
STATUS_FIELDS = ['card_status', 'card_checked_date', 'card_check_reason'] + CONTROL_FIELDS
DAILY_FIELDS = ['collection_status'] + STATUS_FIELDS


class Metadata(HTMLParser):
    def __init__(self, text):
        super().__init__(); self.canonical = ''; self.title = ''; self.in_title = False; self.in_next = False; self.next_data = ''
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'link' and a.get('rel') == 'canonical': self.canonical = a.get('href', '')
        if tag == 'title': self.in_title = True
        if tag == 'script' and a.get('id') == '__NEXT_DATA__': self.in_next = True

    def handle_endtag(self, tag):
        if tag == 'title': self.in_title = False
        if tag == 'script': self.in_next = False

    def handle_data(self, text):
        if self.in_title: self.title += text
        if self.in_next: self.next_data += text


def card_kind(response, master_id):
    try:
        meta = Metadata(response.text)
        hotel = json.loads(meta.next_data)['props']['pageProps']['hotel']
        if str(hotel.get('masterId')) == str(master_id):
            return str(hotel.get('facts', {}).get('kind') or '')
    except (ValueError, KeyError, TypeError):
        pass
    return ''


def classify_card(response, master_id):
    if response.status_code in (404, 410): return 'inactive', f'HTTP {response.status_code}', ''
    if response.status_code != 200: return 'unknown', f'HTTP {response.status_code}', ''
    meta = Metadata(response.text)
    if not meta.title or any(x in meta.title.lower() for x in ['captcha', 'just a moment', 'access denied', 'проверка браузера']):
        return 'unknown', 'unverified_or_blocked_page', ''
    marker = f'/mid{master_id}/'
    if marker in urlparse(meta.canonical).path:
        return 'active', 'hotel_card_verified', meta.canonical
    if marker not in urlparse(response.url).path and '/mid' not in urlparse(response.url).path:
        return 'inactive', 'redirected_outside_hotel', ''
    return 'unknown', 'hotel_identity_not_verified', ''


class CardChecker:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

    def __call__(self, hotel):
        master = str(hotel.get('master_id', '')).removesuffix('.0')
        if not master: return 'unknown', 'missing_master_id', ''
        slug = hotel['ota_hotel_id']
        fallback = f'https://ostrovok.ru/hotel/russia/western_siberia_irkutsk_oblast_multi/mid{master}/{slug}/'
        urls = list(dict.fromkeys([hotel.get('url') or fallback, fallback]))
        results = []
        for url in urls:
            result = ('unknown', 'request_failed', '')
            for _ in range(2):
                try:
                    response = self.session.get(url, timeout=25)
                    result = classify_card(response, master)
                except requests.RequestException:
                    result = ('unknown', 'request_failed', '')
                if result[0] != 'unknown': break
            if result[0] == 'active':
                kind = card_kind(response, master)
                if kind: hotel['kind'] = kind
                return result
            results.append(result)
        # A broken city-specific route alone cannot deactivate a valid property.
        if all(r[0] == 'inactive' for r in results): return results[-1]
        return 'unknown', '; '.join(r[1] for r in results), ''


def reconcile_catalog(existing, observed, checker, today, future_checker=None):
    """Check missing cards and control nights; do not treat errors as deletion or occupancy."""
    future_checker = future_checker or FutureAvailabilityChecker()
    daily = [dict(h, collection_status='search_found', card_status='active',
                  card_checked_date=today, card_check_reason='found_in_search') for h in observed if not is_apartment(h.get('kind'))]
    seen = {h['ota_hotel_id'] for h in observed}
    for hotel_id, hotel in existing.items():
        if is_apartment(hotel.get('kind')):
            hotel['monitoring_scope'] = 'excluded_apartment'
            continue
        if hotel_id in seen:
            hotel.update(card_status='active', card_checked_date=today, card_check_reason='found_in_search')
            continue
        if hotel.get('card_status') == 'inactive': continue
        status, reason, canonical = checker(hotel)
        hotel.update(card_status=status, card_checked_date=today, card_check_reason=reason)
        if canonical: hotel['url'] = canonical
        if is_apartment(hotel.get('kind')):
            hotel['monitoring_scope'] = 'excluded_apartment'
            continue
        hotel['monitoring_scope'] = 'included'
        logger.info('Карточка %s: %s (%s)', hotel_id, status, reason)
        if status != 'inactive':
            row = dict(hotel)
            if status == 'active':
                evidence = future_checker(hotel, date.fromisoformat(today) + timedelta(days=1))
                hotel.update(evidence);row.update(evidence)
                row['collection_status'] = ASSUMED_FULL if evidence['future_availability_status'] == 'available' else ('no_future_offers' if evidence['future_availability_status'] == 'unavailable' else 'future_check_error')
            else:
                row['collection_status'] = 'card_check_error'
            daily.append(row)
    return daily
