<div align="center">

# OwenCloud Bot

**Telegram-бот для [OwenCloud](https://web.owencloud.ru/): уведомления о событиях приборов ОВЕН,
текущие параметры и запись управляемых параметров.**

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![aiogram](https://img.shields.io/badge/aiogram-3-2CA5E0?logo=telegram&logoColor=white)
![SQLite / PostgreSQL](https://img.shields.io/badge/SQLite%20%7C%20PostgreSQL-003B57?logo=sqlite&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)

</div>

## Возможности

- Уведомления о начале и окончании событий журнала OwenCloud; выбор событий по прибору: аварии, все, список.
- Отметка аварий прочитанными: кнопка у сообщения или все аварии прибора из его карточки; синхронизация с OwenCloud.
- Текущие параметры по категориям OwenCloud; запись управляемых параметров с подтверждением и контролем статуса.
- Несколько аккаунтов OwenCloud в чате, ключи в БД зашифрованы; доступ по коду (`ACCESS_CODE`).
- Очередь недоставленных уведомлений с повторами; прокси SOCKS5/SOCKS4/HTTP с авторизацией.
- SQLite, внешний PostgreSQL или PostgreSQL в контейнере; перенос между базами и резервные копии.

```
🔴 Котельная — авария                 ↪ ✅ Котельная — авария завершена
Не удалось поджечь                       Не удалось поджечь
🕒 20.02.2026 14:53                       🕒 14:53 → 15:09 (16 мин)
[ Отметить прочитанным ]
```

🔴/✅ — начало/окончание аварии, 🔵/⚪ — информационного события. Кнопка «Отметить прочитанным» — только у аварий,
одна на событие; окончание приходит ответом на начало.

## Запуск

Образ: [`whitemanprk/owen_bot`](https://hub.docker.com/r/whitemanprk/owen_bot).

```bash
sudo mkdir -p /opt/owenbot && cd /opt/owenbot
sudo curl -fsSLO https://raw.githubusercontent.com/WhiteManPrk/Owen_Bot/main/docker-compose.yml
sudo curl -fsSLO https://raw.githubusercontent.com/WhiteManPrk/Owen_Bot/main/docker-compose.postgres.yml
sudo curl -fsSL -o .env https://raw.githubusercontent.com/WhiteManPrk/Owen_Bot/main/.env.example
sudo chmod 600 .env
# ENCRYPTION_KEY:
sudo docker run --rm whitemanprk/owen_bot python -c \
  "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Варианты базы (`.env`):

| Вариант | Compose-файл | `.env` |
|---|---|---|
| SQLite | `docker-compose.yml` | `DB_BACKEND=sqlite` |
| Внешний PostgreSQL | `docker-compose.yml` | `DB_BACKEND=postgres`, `POSTGRES_HOST=<адрес>`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` |
| PostgreSQL в контейнере | `docker-compose.postgres.yml` | `COMPOSE_FILE=docker-compose.postgres.yml`, `DB_BACKEND=postgres`, `POSTGRES_HOST=db`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` |

Внешний PostgreSQL: база создана заранее, пользователь — её владелец (таблицы создаются миграциями при запуске),
сервер доступен из контейнера. PostgreSQL на том же хосте — `POSTGRES_HOST=host.docker.internal` и `extra_hosts`
в `docker-compose.yml`. В контейнерном варианте `db` создаётся с теми же `POSTGRES_DB/USER/PASSWORD`, данные — `./pgdata`.

```bash
sudo docker compose up -d
sudo docker compose logs -f bot
sudo docker compose pull && sudo docker compose up -d   # обновление
```

При неполных настройках базы бот не запускается и пишет в лог, какие переменные задать.

В Telegram: `/start` → код доступа (если задан) → API-ключ OwenCloud (профиль → API-ключ) → `/devices` →
прибор → «Включить уведомления».

## Переменные окружения

| Переменная | Обяз. | По умолчанию | Описание |
|---|:-:|---|---|
| `BOT_TOKEN` | ✅ | | Токен бота (@BotFather) |
| `ENCRYPTION_KEY` | ✅ | | Ключ Fernet для API-ключей OwenCloud. При потере ключи придётся добавить заново |
| `DB_BACKEND` | ✅ | | `sqlite` или `postgres` |
| `POSTGRES_HOST` / `POSTGRES_PORT` | для postgres | — / `5432` | Сервер PostgreSQL (`db` — контейнер из `docker-compose.postgres.yml`) |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | для postgres | | База и учётная запись |
| `ACCESS_CODE` | | | Код доступа; пусто — бот открыт |
| `TG_PROXY` | | | `socks5://`, `socks4://`, `http://` + `host:port` |
| `TG_PROXY_LOGIN` / `TG_PROXY_PASSWORD` | | | Авторизация прокси Telegram |
| `OWEN_API_URL` | | `https://api.owencloud.ru/v1` | API OwenCloud |
| `OWEN_PROXY` | | | `socks5://`, `socks5h://`, `http://`, `https://` |
| `OWEN_PROXY_LOGIN` / `OWEN_PROXY_PASSWORD` | | | Авторизация прокси OwenCloud |
| `POLL_INTERVAL` | | `60` | Опрос журнала, с |
| `OUTBOX_TTL_HOURS` | | `24` | Срок хранения недоставленного уведомления, ч |
| `BACKUP_EVERY_HOURS` / `BACKUP_KEEP` | | `0` / `7` | Автокопии: период, ч (0 — выкл.); число копий |
| `LOG_LEVEL` | | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_ACTIONS_UNTIL` | | | Журнал нажатий и команд пользователей в INFO до даты `ГГГГ-ММ-ДД` включительно |

Логи: `docker compose logs bot` (3 × 10 МБ) и `/logs/owenbot.log` в контейнере (5 × 10 МБ);
на хост — том `./logs:/logs` в compose-файле.

Доступ по коду: код запрашивается в каждом новом чате, 5 неверных попыток — блокировка на 15 минут.
После смены кода чаты вводят его заново.

## База данных

Схема обновляется миграциями при запуске. Команды `dbtool` выполняются в контейнере бота; для переноса
должны быть заданы настройки обеих баз.

```bash
sudo docker compose exec bot python -m app.dbtool info
sudo docker compose exec bot python -m app.dbtool backup            # ./data/backups, без остановки

sudo docker compose stop bot
sudo docker compose run --rm bot python -m app.dbtool restore /data/backups/owenbot-YYYYMMDD-HHMMSS.db
sudo docker compose run --rm bot python -m app.dbtool copy --from sqlite --to postgres
sudo docker compose up -d
```

- Резервная копия — файл SQLite независимо от базы; восстанавливается в любую. Нужен тот же `ENCRYPTION_KEY`.
- `copy` и `restore` требуют пустую целевую базу (`--force` — перезаписать), выполняются в одной транзакции
  и сверяют число строк. Исходная база не меняется.

## Команды бота

| Команда | Действие |
|---|---|
| `/start` | Начало работы, справка |
| `/devices` | Приборы: статус, параметры, уведомления, управление, прочтение аварий |
| `/accounts` | API-ключи OwenCloud |
| `/cancel` | Отменить ввод |

Статусы: 🔴 авария · 🟠 непрочитанные аварии · 🟢 на связи · ⚫ нет связи · 🔔 уведомления включены.

## Работа

- Поллер каждые `POLL_INTERVAL` с читает журнал приборов с подписками, с перекрытием 1 ч; запись журнала
  обрабатывается один раз, в том числе после перезапуска. После паузы больше 6 ч старые события не рассылаются.
- Уведомления идут через очередь в БД: по порядку в каждом чате, с повторами до `OUTBOX_TTL_HOURS`;
  опоздавшие помечаются «⏳ доставлено с задержкой».
- Ключ, отклонённый OwenCloud, исключается из опроса, владелец получает сообщение.

## Безопасность

- API-ключи в БД — Fernet (AES-128-CBC + HMAC-SHA256); код доступа — HMAC-SHA256 с `ENCRYPTION_KEY`.
- Сообщения с ключом и кодом удаляются из чата; текст сообщений в логи не пишется.
- Подписка только на приборы, видимые ключу; данные кнопок проверяются.
- Файлы SQLite и резервные копии — права 600; контейнер PostgreSQL порт не публикует; входящих портов нет.
- Запись параметров — от имени владельца ключа, после подтверждения. В групповом чате управлять может любой участник.

## Разработка

```
app/
  main.py        запуск бота и поллера
  config.py      переменные окружения
  owen.py        клиент OwenCloud API
  db.py          запросы к данным (SQLite и PostgreSQL)
  storage/       адаптеры баз, миграции
  dbtool.py      перенос, резервные копии, восстановление
  poller.py      опрос журнала, начало/окончание событий
  notifier.py    очередь и доставка уведомлений
  formatting.py  тексты уведомлений и параметров
  bot/           обработчики Telegram
tests/           pytest: SQLite и PostgreSQL, заглушки OwenCloud и Telegram
```

```bash
docker compose -f docker-compose.test.yml run --rm --build tests

VERSION=1.3.1
docker build --platform linux/amd64 --build-arg VERSION=$VERSION \
  -t whitemanprk/owen_bot:$VERSION -t whitemanprk/owen_bot:latest .
docker push whitemanprk/owen_bot:$VERSION && docker push whitemanprk/owen_bot:latest
```

---

<sub>Проект разработан с использованием ИИ.</sub>
