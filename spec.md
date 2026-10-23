# Спека: Decision Journal — трекер решений

## Суть
Журнал решений с фальсифицируемыми прогнозами: записал решение + прогнозы с датой ревью → в срок закрыл (hit/miss/partial) → калибровка по графику. Форма ввода — опросник: веб (wizard/быстрая форма) и ТГ-бот (FSM) повторяют один и тот же сценарий.

## Архитектура — веб-приложение, не PyQt
Бот потом = тот же backend, другой frontend. Если делать PyQt — придётся дублировать логику в боте. При вебе `db.py` переиспользуется как есть, бот просто дёргает те же функции. Бонус: с телефона через локалку открывается тот же интерфейс.

## Стек
FastAPI + SQLite + Jinja2 + Alpine.js + Pico.css + Chart.js. Ноль билд-степа, вся статика — локальная (офлайн).

- Python ≥ 3.10 (в проекте venv на 3.13).
- Windows-нюанс: `python` в PATH — msys2 без pip. Всегда использовать venv: `.venv\Scripts\python.exe`.
- Пакеты (`requirements.txt`): fastapi, uvicorn, jinja2, python-multipart (POST-формы), itsdangerous (сессии), pystray + Pillow (трей), httpx (тесты), pytest, pyinstaller (сборка). На macOS CI дополнительно ставит `pyobjc-framework-Cocoa`.

## Структура
```
decisionapp/
├── db.py              # схема + CRUD (используется вебом, ботом и тестами)
├── auth.py            # пароль: PBKDF2-хеш в auth.json, session_secret
├── app.py             # FastAPI routes + auth-middleware
├── main.py            # entry exe: single-instance, uvicorn в потоке, трей
├── tray.py            # трей-иконка: Открыть / Новая запись / Выключить
├── scripts/
│   └── make_icon.py   # генерация static/icon.png + icon.ico (Pillow)
├── templates/
│   ├── base.html      # шапка, nav, стили (светлая баухаус-тема)
│   ├── index.html     # список + просроченные review
│   ├── new.html       # форма нового решения (quick + wizard)
│   ├── view.html      # решение + прогнозы + закрытие + ретроспектива + удаление
│   ├── edit.html      # правка полей решения
│   ├── calibration.html
│   ├── login.html     # вход (standalone)
│   └── setup.html     # первый запуск, задать пароль (standalone)
├── static/
│   ├── style.css      # баухаус-тема поверх Pico
│   ├── icon.png, icon.ico  # иконка трея и exe
│   └── vendor/        # pico.min.css, alpine.min.js, chart.umd.js, fonts/ + fonts.css
├── tests/
│   └── test_db.py     # pytest на db.py
├── .github/
│   └── workflows/build-macos.yml  # CI: сборка .app на macos-latest
└── dist/              # артефакты сборки (в git не входят)
```
БД рядом с `db.py` — только в dev; в exe — системная папка данных (см. «Путь к БД» ниже).

## Схема БД (структура процесса принятия решений)
```sql
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    title TEXT NOT NULL,
    chosen TEXT NOT NULL,
    context TEXT,
    reasoning TEXT,
    alternatives TEXT,
    wrong_if TEXT,
    stakes INTEGER CHECK(stakes BETWEEN 1 AND 3),
    reversibility TEXT CHECK(reversibility IN ('one-way','two-way') OR reversibility IS NULL),
    tags TEXT,
    retrospective TEXT
);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY,
    decision_id INTEGER NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    confidence REAL NOT NULL CHECK(confidence BETWEEN 1.0 AND 10.0),
    review_at TEXT NOT NULL,
    outcome TEXT CHECK(outcome IN ('hit','miss','partial') OR outcome IS NULL),
    outcome_quality INTEGER CHECK(outcome_quality BETWEEN -2 AND 2 OR outcome_quality IS NULL),
    outcome_at TEXT,
    outcome_notes TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pred_review ON predictions(review_at) WHERE outcome IS NULL;
CREATE INDEX IF NOT EXISTS idx_pred_decision ON predictions(decision_id);
```

- `alternatives` — что ещё рассматривал и почему отверг (через полгода самое ценное: был ли выбор вообще).
- `wrong_if` — при каких признаках решение ошибочно / что изменит моё мнение. Не то же, что прогнозы: прогнозы — вероятностные следствия, это — явное условие провала.
- Путь к БД: env `DECISIONS_DB` главнее всего; в dev — рядом с `db.py`; в exe — системная папка данных: Windows `%LOCALAPPDATA%\DecisionJournal`, macOS `~/Library/Application Support/DecisionJournal`, Linux `~/.local/share/DecisionJournal` — единая база независимо от того, откуда запущен exe. Нужно для тестов и бота.
- `PRAGMA foreign_keys = ON` на каждом соединении (иначе CASCADE не работает).
- Шкала уверенности: **1.0–10.0 с шагом 0.5** (осознанный выбор, не проценты).
- Прогнозы **неизменяемы**: не редактируются и не удаляются по отдельности — иначе калибровка подрывается задним числом.

## db.py — API (контракт для веба и бота)
- `init_db()` — создаёт таблицы; перед этим, если БД с данными уже есть — автобэкап с ротацией 3.
- `create_decision(data, predictions) -> int` (атомарно), `get_decision(did) -> (dict | None, list[dict])`.
- `list_decisions(limit=100, tag=None)` — с `pred_count`, `closed_count` (COALESCE), `next_review`; фильтр по тегу — точный токен, не подстрока.
- `update_decision(did, fields) -> str` — `'updated' | 'not_found' | 'no_changes'` (различаем «нет такого решения» и «нечего менять»); только title/chosen/context/reasoning/alternatives/wrong_if/stakes/reversibility/tags.
- `delete_decision(did) -> bool` — каскадно сносит прогнозы.
- `add_retrospective(did, text) -> bool`.
- `due_predictions()` — незакрытые с `review_at <= сегодня`; сортировка: stakes DESC, затем review_at ASC.
- `open_predictions(decision_id)`, `get_prediction(pid)`.
- `close_prediction(pid, outcome, quality, notes) -> bool` — False, если прогноза нет или он уже закрыт.
- `calibration_bins()` — 5 дискретных бинов по диапазонам значений шкалы: [1.0–2.5], [3.0–4.5], [5.0–6.5], [7.0–8.5], [9.0–10.0] (включительно, без пересечений: 19 значений = 4+4+4+4+3); `hit` = 1.0, `partial` = 0.5, `miss` = 0.0.
- `brier_score() -> dict | None` — `{"value": mean((conf/10 − outcome_score)²), "n": <закрытых>, "reliable": n ≥ 20}`; UI при n<20 честно пишет «мало данных».
- `export_rows()` — решения × прогнозы одной пачкой.
- Валидация живёт здесь (общая для веба и бота): confidence float 1.0–10.0, review_at ISO YYYY-MM-DD, теги нормализуются (strip + lowercase, дедуп), пустые опциональные поля → NULL.

## Безопасность
- Пароль обязателен всегда (и на 127.0.0.1 — чтобы привычка не подвела при `--host 0.0.0.0`).
- Первый запуск → `/setup` (пароль + подтверждение), затем авторизация; повторный `/setup` перезаписать пароль не может (guard: редирект на /login).
- Хеш PBKDF2-SHA256 (200k итераций) + salt в `auth.json` рядом с БД; там же `session_secret`. Забыл пароль → удали `auth.json`, задай заново.
- `SessionMiddleware`: cookie HttpOnly, SameSite=Lax, 30 дней.
- http-middleware пропускает только `/login`, `/setup`, `/logout`, `/static` — всё остальное требует сессии. SessionMiddleware добавляется ПОСЛЕ auth-middleware (внешним), иначе `request.session` недоступен.
- Задержка 0.5с при неверном пароле.
- Сеть: по умолчанию `127.0.0.1`. `--host 0.0.0.0` (телефон/локалка) — осознанно; HTTPS локально не поднимаем, пароль — защита; секретов в базе не держать.
- Бэкапы: при каждом старте — автобэкап БД с ротацией 3 (`decisions.backup.db` → `.1` → `.2`, свежий всегда `.backup.db`), копирование через sqlite backup API (корректно при WAL); ручной бэкап = копия `decisions.db` (+ `auth.json`, `bot.json`).
- Фаза 2 (бот): long polling — только исходящие соединения; токен в `bot.json` вне git; owner-allowlist (первый `/start`), чужие игнорируются.

## Опросник — единый для веба и бота
Поля и порядок шагов одинаковы: title → chosen → context(ск) → reasoning(ск) → alternatives(ск) → wrong_if(ск) → stakes → reversibility → теги(ск) → прогнозы. «(ск)» = optional, кнопка «Пропустить». У полей — подписи-подсказки под инпутом (не исчезают при наборе): chosen — «что именно делаешь»; context — «что триггернуло»; reasoning — «зафиксируй аргументы до того, как известен исход» + placeholder-pre-mortem «Ожидаю X, потому что Y. Что может пойти не так»; alternatives — «через полгода покажет, был ли выбор»; wrong_if — «явное условие провала». В боте те же формулировки — в текстах шагов.

## app.py — маршруты
- `GET /` — due-карточки + таблица решений; фильтр `?tag=money`.
- `GET /new`, `POST /new` → create + redirect (PRG). Прогнозы приходят списками `pred_text[]` / `pred_conf[]` / `pred_review[]` — в FastAPI только через `Form(alias="pred_text[]")` (alias обязателен, иначе поля молча теряются).
- `GET /decision/{id}`, `GET /decision/{id}/edit` + `POST` (404 только при `not_found`; `no_changes` — обычный redirect), `POST /decision/{id}/delete` (каскад).
- `POST /prediction/{id}/close` → 404 если нет/закрыт.
- `POST /decision/{id}/retrospective`.
- `GET /calibration`, `GET /export.csv` (UTF-8 BOM для Excel), `GET /export.json`.
- `GET/POST /login`, `GET/POST /setup`, `POST /logout`, `POST /exit` (гасит сервер из exe; в dev — 400).
- `GET /healthz` — публичный (без авторизации): `{"app": "decision-journal", "db": <путь>}` — для single-instance проверки и диагностики «а какая база открыта».
- Инициализация БД через lifespan; `StaticFiles`/`templates` от `BASE_DIR` (`sys._MEIPASS` в exe).

## Страницы
### index.html
- Сверху карточки просроченных прогнозов (красная рамка), отсортированы по stakes → дате. Кнопка «Закрыть» ведёт на `view.html#pred-{id}`.
- Ниже таблица решений: дата, title, closed/pred_count, next_review. Чипы тегов сверху — фильтр.

### new.html
- Два режима: **Быстрый** (по умолчанию) и **Пошагово** (Alpine wizard: шаг = один вопрос, «Далее/Пропустить») — тот же порядок, что в FSM бота.
- Прогнозы: динамический список (Alpine, состояние встроено в `x-data` формы — без `Alpine.data`/`alpine:init`, чтобы не зависеть от порядка скриптов): текст + уверенность (1.0–10.0, шаг 0.5) + дата ревью.
- **Пресеты даты ревью**: кнопки «+1 нед / +1 мес / +3 мес / +1 год» проставляют дату **всем** строкам (перезаписывают).
- `<noscript>`-баннер: без JS кнопки дат и список прогнозов не работают — предупреждение видно.

### view.html
- Карточка решения (все поля) + прогнозы по `review_at`.
- Незакрытый прогноз: форма закрытия — radio hit/miss/partial, quality (−2…+2), notes.
- Все закрыты → блок «Ретроспектива» с тремя подсказками: «что реально произошло», «что сделал бы иначе», «правило на будущее».
- Кнопки «Редактировать» и «Удалить» (JS confirm, каскад).

### calibration.html
- Chart.js bar: «Заявлено» (avg_conf, красный) vs «Реально» (hit_rate, синий). Синий выше — недоуверен, ниже — переуверен.
- Сверху **Brier score** цифрой; таблица бинов с `n`.

### login.html / setup.html
- Standalone-страницы в той же теме: карточка по центру, пароль (setup — с подтверждением).

## Дизайн — баухаус, светлая тема
- Палитра: фон бежевый `#F2EAD9`, карточки `#FBF6EC`, текст `#2B2620`, мягко-оранжевый `#E8A268`, мягко-красный `#D9604A` (primary), приглушённый `#857763`.
- Реализация: переопределение Pico-переменных (`--pico-*`) в `:root` + свои правила в `style.css`: скругления → 0, рамки 2px + offset-тень `4px 4px 0`, uppercase-заголовки с трекингом, шрифт **Jost** (Futura-подобный, есть кириллица, 400/600/700 woff2 локально).
- Геометрический логотип в шапке: квадрат (красный), круг (оранжевый), треугольник (CSS borders).
- Статусы: hit → оранжевый, partial → бежево-оливковый, miss → красный; due-карточки — красная левая рамка; ретроспектива — оранжевая.

## Офлайн-статика
`static/vendor/`: pico.min.css, alpine.min.js, chart.umd.js, `fonts/*.woff2` + сгенерированный `fonts.css` (unicode-range для latin/cyrillic). Шаблоны не ходят в CDN — exe работает без интернета. Страховка MIME: `mimetypes.add_type()` для .js/.css/.woff2 (реестр Windows на другой машине может отдать мусорный Content-Type, и браузер молча не выполнит JS).

## Трей и жизненный цикл exe
- Сервер живёт, пока работает процесс (он нужен будущему боту); закрытие вкладок его **не** гасит.
- Трей-иконка (pystray, иконка `static/icon.png`): **Открыть** (вкладка на главную; действие по двойному клику), **Новая запись** (вкладка на `/new`), **Выключить** (`icon.stop()` + `server.should_exit` → чистое завершение).
- `main.py`: uvicorn в daemon-потоке, `icon.run()` в главном потоке (требование macOS AppKit); после выхода из трея — join серверного потока (5с). Если pystray недоступен — fallback: сервер в главном потоке без трея.
- Single-instance: второй запуск находит живой экземпляр по `/healthz` (порты 8000…8020) и просто открывает вкладку — никаких двух серверов и двух баз.
- Кнопка «Завершить» в шапке (видна только в exe) — дублирует «Выключить». Death-watch поток следит за uvicorn: как только сервер гаснет (из веба или из трея), иконка убирается автоматически — «нажал Завершить в браузере → иконка исчезла».

## Сборка Windows (exe)
`pyinstaller --onefile --noconsole --icon static\icon.ico --name DecisionJournal --add-data "templates;templates" --add-data "static;static" main.py` → `dist\DecisionJournal.exe` (~23 MB, внутри Pillow+pystray). Guard на None-stdout/stderr для `--noconsole`. БД и `auth.json` — в системной папке данных, первый запуск открывает `/setup`.

## Сборка под macOS
- Кросс-сборки не существует: только на mac или в CI.
- GitHub Actions: репо https://github.com/Courtesyl/DecisionJournal (public), workflow `.github/workflows/build-macos.yml` — собирается автоматически на каждый пуш в master + вручную (workflow_dispatch); macos-latest: Python 3.13 → `pip install -r requirements.txt pyobjc-framework-Cocoa` → `pyinstaller --onedir --windowed --add-data "templates:templates" --add-data "static:static"` (onefile+windowed на mac — deprecated, .app не может быть одним файлом) → zip `DecisionJournal-macos.zip` → артефакт в Actions. На macOS разделитель в `--add-data` — `:`.
- Gatekeeper на чужом маке: первый запуск — «приложение от неустановленного разработчика» → правый клик по .app → «Открыть» (или `xattr -cr DecisionJournal.app`). Постоянная подпись ($99/год) для личного инструмента не нужна.

## Энергоэффективность
- **Один event-loop на всё** (фаза 2): uvicorn + aiogram polling + дайджест-планировщик в одном asyncio-цикле одного потока — ноль busy-wait, ноль лишних потоков и переключений.
- **Дайджест — событийный**: планировщик спит (`asyncio.sleep`) до ближайшего `review_at` и пересчитывается после каждого create/close — никаких опросов БД по таймеру.
- **SQLite в WAL**: `journal_mode=WAL` + `synchronous=NORMAL` — быстрее запись, читатели не блокируют писателей (важно, когда веб и бот работают одновременно).
- Сеть к Telegram: keep-alive сессия aiogram, long poll держит открытое соединение — ожидание стоит ноль CPU.
- Веб уже событийный: сервер спит на IO, вкладка — Alpine, ни одного поллера.
- Логи: `log_level=warning` (уже есть), access-логи не ведём.

## Тесты
- pytest (`tests/test_db.py`, 33 шт.) через env `DECISIONS_DB` на tmp: CRUD, фильтр тегов, due-сортировка, close/двойное закрытие, удаление с каскадом, дискретные бины (partial=0.5, границы включительно, сумма n = всем значениям), brier (value/n/reliable≥20), update_decision (3 состояния), автобэкап (создание + ротация 3).
- Смок (TestClient, в одном процессе — никаких фоновых серверов): setup/login/логаут, неверный пароль → 401, защита повторного setup, весь CRUD-флоу, удаление, экспорт, wizard.

## ТГ-бот (фаза 2) — главный интерфейс опросника
aiogram 3, тот же `db.py`, та же БД. Чат — естественная форма опросника: один вопрос = одно сообщение. Бот живёт **только пока запущен exe** (таск в том же процессе, без автозагрузки): дайджесты приходят, пока приложение работает.

### Безопасность бота
- **Long polling only** (`getUpdates`): только исходящие HTTPS к api.telegram.org — ноль входящих портов, машина невидима снаружи. Webhook не используем (требует публичный URL, TLS и открытый порт — лишняя поверхность атаки).
- **Токен**: файл `bot.json` рядом с БД (в git не попадает); утечка → BotFather `/revoke` → новый токен; в логи и скриншоты не светить.
- **Owner allowlist**: первый `/start` фиксирует `user_id` владельца в `bot.json`; чужие апдейты игнорируются молча — ни создавать записи, ни получать данные они не могут.
- **Приватность контента**: чат с ботом хранится на серверах Telegram (без end-to-end) — осознанно принимаем, ограничений на контент нет; локальная БД при этом не покидает ПК.
- Бот в том же процессе, что и веб — нового сетевого периметра не появляется.

### FSM: /new
Шаг = один вопрос, у optional-шагов кнопка «Пропустить», `/cancel` — отмена. Минимум ручного ввода — всё в кнопках.
1. «Что решил?» → title
2. «Что именно выбираешь?» → chosen
3. «Что триггернуло?» → context (ск)
4. «Почему? Аргументы, цифры» → reasoning (ск)
5. «Какие альтернативы рассматривал? Почему отверг?» → alternatives (ск)
6. «При каких признаках решение ошибочно?» → wrong_if (ск)
7. «Масштаб ставки» → кнопки: мелкая / средняя / крупная
8. «Обратимость» → кнопки: two-way / one-way
9. «Теги через запятую» → tags (ск)
10. Прогнозы (цикл, по одному): текст → уверенность (кнопки 1…10, 2 ряда по 5) → дата (+1нед/+1мес/+3мес/+1год/своя; валидация YYYY-MM-DD или ДД.ММ) → «Ещё прогноз / Готово»
11. Сводка + «Сохранить» / «Исправить последний шаг» → `create_decision`, ответ с id.

### FSM: закрытие прогноза (из digest)
Пуш due-прогноза → кнопки hit/miss/partial → quality: −2…+2 + «—» → notes (ск) → готово.

### Прочее
- Ежедневный digest: due-прогнозы с кнопкой «Закрыть».
- Ретроспектива: когда все прогнозы решения закрыты — приглашение заполнить (те же три подсказки).
- Не хватает db.py — добавлять функции туда, не дублировать SQL в боте.

## Порядок работ
1. ~~db.py + тесты~~ ✅
2. ~~app.py + шаблоны + калибровка + дизайн~~ ✅
3. ~~auth + удаление + баухаус + офлайн-статика~~ ✅
4. ~~exe + auth + единая БД + автобэкап~~ ✅
5. ~~трей-иконка (pystray) + иконка exe~~ ✅
6. ~~GitHub Actions: сборка под macOS~~ ✅ репо + workflow запушены
7. Ручной прогон глазами (шрифты/цвета/вопросы UX).
8. Фаза 2: ТГ-бот.
