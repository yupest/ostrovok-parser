## Структура репозитория

```
.
├── ostrovok_hotels.py        # Шаг 1: парсинг списка отелей + каталог
├── ostrovok_rooms.py         # Шаг 2: парсинг номеров и цен
├── ostrovok_statistic.py     # Шаг 3: сводная статистика за день
├── capacity_utils.py         # Оценка вместимости номера
├── log_config.py             # Логирование + Telegram-уведомления
├── daily/
│   ├── hotels/               # YYYY-MM-DD.csv — отели за день
│   ├── rooms/                # YYYY-MM-DD.csv — номера за день
│   └── statistics/           # YYYY-MM-DD.csv — статистика за день
├── catalog/
│   └── hotels.csv            # Накопительный каталог отелей
├── logs/                     # Логи запусков
└── .github/workflows/
    └── ostrovok-parser.yml   # Ежедневный запуск в GitHub Actions
```

## Форматы данных

### `daily/hotels/YYYY-MM-DD.csv`

| Колонка | Описание |
|---|---|
| `city` | Город |
| `ota_hotel_id` | Идентификатор отеля в Ostrovok |
| `master_id` | Master ID отеля |
| `name` / `name_en` | Название (рус./англ.) |
| `address` | Адрес |
| `latitude` / `longitude` | Координаты |
| `url` | Ссылка на страницу отеля |
| `rooms_number` | Общее число номеров в отеле |

### `daily/rooms/YYYY-MM-DD.csv`

| Колонка | Описание |
|---|---|
| `ota_hotel_id`, `master_id` | Идентификаторы отеля |
| `room_name` | Название номера |
| `rg_hash` | Хеш группы номеров (ключ группировки) |
| `count_rg_hash` | Сколько тарифов объединено в группу |
| `allotment` | Кол-во свободных номеров этого типа |
| `bedding_type`, `beds`, `bedding_data`, `multi_bed_data` | Информация о кроватях |
| `capacity` | Оценённая вместимость одного номера (гостей) |
| `price_rub_min` / `price_rub_max` | Мин./макс. цена за ночь, ₽ |
| `url` | Ссылка на страницу отеля |

Если по отелю API не вернул тарифов, в файл пишется строка-заглушка с пустым `rg_hash` — для контроля пропусков.

### `daily/statistics/YYYY-MM-DD.csv`

| Колонка | Описание |
|---|---|
| `ota_hotel_id`, `name` | Отель |
| `date` | Дата сбора |
| `rooms_num` | Всего номеров в отеле |
| `free_rooms_amount` | Сумма allotment по всем типам номеров |
| `max_capacity` | Суммарная вместимость свободных номеров |
| `available_rooms_percent` | Доля свободных номеров, % |
| `min_price` | Минимальная цена за ночь, ₽ |

### `catalog/hotels.csv`

Те же поля, что и в дневном файле отелей, плюс `first_seen_date` и `last_seen_date` — когда отель впервые и в последний раз встречался в выдаче.



## Полнота поиска и ошибки пагинации

Поиск выполняется без `type_group`: собираются все типы размещения, включая квартиры,
гостевые дома и коттеджи. Это меняет состав выборки по сравнению со старой версией.
Даты проживания: заезд завтра, выезд послезавтра, один взрослый.

Парсер накапливает порции API до `search_finished=true`, затем сверяет число уникальных
объектов с `total_av_hotels`. Пустой промежуточный ответ не является концом списка.
HTTP-ошибки, некорректный JSON, незавершённый поиск, повтор страницы без новых объектов
и достижение лимита страниц приводят к повтору, а затем к ошибке запуска. Неполный
сбор отелей не сохраняется, следующие этапы workflow не выполняются.
Диагностика каждой попытки: `logs/YYYY-MM-DD-search.json`. При успешном запуске
логи коммитятся вместе с данными; при ошибке сообщения доступны в журнале Actions. Ключи сессий и cookies в диагностику не записываются.
Это проверка полноты относительно числа, сообщённого API, а не гарантия наличия
всех работающих средств размещения региона. Поиск по-прежнему зависит от дат и гостей.

Настройки: `SEARCH_PAGE_TIMEOUT_MS=90000`, `SEARCH_PAGE_RETRIES=3`,
`SEARCH_MAX_PAGES=500`. Достижение лимита не считается успешным концом выдачи.

## Локальный запуск

```sh
/opt/anaconda3/bin/python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
PLAYWRIGHT_BROWSERS_PATH=.playwright .venv/bin/python -m playwright install chromium
.venv/bin/python -m unittest discover -s tests -v
PLAYWRIGHT_BROWSERS_PATH=.playwright .venv/bin/python ostrovok_hotels.py
PLAYWRIGHT_BROWSERS_PATH=.playwright .venv/bin/python ostrovok_rooms.py
.venv/bin/python ostrovok_statistic.py
```

## GitHub Actions

Workflow `Ostrovok Parser` запускается ежедневно в 08:00 UTC (16:00 по Иркутску),
и вручную через Run workflow. Отправка кода сама по себе не запускает сбор.
Тесты можно запустить локально командой выше. Для коммита результатов workflow
должен иметь право записи Contents (Settings → Actions → General → Workflow permissions).
Секрет `PARSERS_REPO_TOKEN` остаётся нужен для существующей доставки в MALISMF/parsers;
без него сбор сохраняется в этом репозитории, а внешняя доставка пропускается.
Telegram-секреты необязательны. У репозитория должен быть включён GitHub Actions.
