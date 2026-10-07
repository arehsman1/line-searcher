# LINE SEARCHER

**Production Telegram bot for searching large TXT datasets stored on Telegram.**

- Streaming / line-based search (multi-GB safe, no full-file RAM load)
- Automatic Free registration — no `/allow` required for Free users
- Configurable Free / Starter / Standard / Pro / Unlimited plans
- Public Free Storage + private storage mapping
- Job queue, live progress, cancellation, restart recovery
- Admin panel, broadcast system, maintenance mode
- Local Bot API Server support for files up to ~2 GB

Support contact: **@Arehsmanbot**

---

## Architecture

```
Telegram Bot Layer (aiogram 3)
        ↓
Auth / User Layer (users.json, modular)
        ↓
Plan & Access Layer
        ↓
Storage Layer (Public Free + Private mapping)
        ↓
File Catalog / Index
        ↓
Search Queue → Search Workers (streaming engine)
        ↓
Result Manager (split, deliver, retain, cleanup)
        ↓
Broadcast Worker (separate, rate-limited)
        ↓
Cleanup / Recovery Worker
```

---

## Telegram file size limits (verified 2026)

| Path | Standard Bot API | Local Bot API Server |
|------|------------------|----------------------|
| Download (`getFile`) | **20 MB** | **Unlimited (~2 GB practical)** |
| Upload (`sendDocument`) | **50 MB** | **2 GB** |

**LINE SEARCHER requires the Local Bot API Server** for multi-hundred-MB / multi-GB source files.

---

## Important limitation: catalog indexing

The **standard Bot API cannot list full channel history** (no `getChatHistory`).

**What works out of the box:**

1. Bot is **admin** in the Public Free Storage channel/group.
2. New `.txt` documents posted to that channel are **auto-indexed**.
3. Refresh verifies existing catalog entries still resolve via `file_id`.

**For bulk historical archives already in a channel:**

- Option A: Re-upload / forward TXT files into the storage channel after the bot is admin.
- Option B (advanced): Add a one-time **MTProto indexer** (Telethon/Pyrogram) using a *bot owner* session — never end-user credentials. This is optional and isolated; core search still uses Bot API + Local Bot API.

This limitation is documented; core search, jobs, plans, and delivery are fully implemented.

---

## Quick start (Ubuntu 24.04)

### 1. System user & directories

```bash
sudo useradd -r -m -d /opt/line-searcher -s /bin/bash linesearcher
sudo mkdir -p /opt/line-searcher /var/lib/telegram-bot-api /tmp/telegram-bot-api
sudo chown -R linesearcher:linesearcher /opt/line-searcher /var/lib/telegram-bot-api
```

### 2. Install Local Bot API Server

**Option A — Docker (recommended):**

```bash
cd /opt/line-searcher
# place API_ID / API_HASH in .env
docker compose -f deploy/docker-compose.bot-api.yml up -d
```

**Option B — Binary from [tdlib/telegram-bot-api](https://github.com/tdlib/telegram-bot-api):**

Build or install the binary, then enable `deploy/telegram-bot-api.service`.

Obtain `API_ID` and `API_HASH` from https://my.telegram.org (app credentials).

### 3. Python environment

```bash
cd /opt/line-searcher
sudo -u linesearcher python3 -m venv venv
sudo -u linesearcher ./venv/bin/pip install -r requirements.txt
```

Copy application code into `/opt/line-searcher/` (this repository).

### 4. Configuration

```bash
cp .env.example .env
nano .env
```

Required:

```env
BOT_TOKEN=...
ADMIN_TELEGRAM_IDS=123456789
API_ID=...
API_HASH=...
USE_LOCAL_BOT_API=true
LOCAL_BOT_API_URL=http://127.0.0.1:8081
DATA_DIR=/opt/line-searcher/data
LOG_DIR=/opt/line-searcher/logs
TIMEZONE=UTC
```

### 5. systemd

```bash
sudo cp deploy/line-searcher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-bot-api   # if using systemd unit
sudo systemctl enable --now line-searcher
sudo systemctl status line-searcher
journalctl -u line-searcher -f
```

### 6. Configure Public Free Storage

1. Create a Telegram channel/group.
2. Add the bot as **administrator** (post messages / access files).
3. In the bot: `/admin` → 📂 STORAGE → ➕ SET PUBLIC STORAGE.
4. Send chat ID (e.g. `-100…`) or forward a message from the channel.
5. Set display name, then **Refresh**.
6. Upload `.txt` files to the channel — they are auto-indexed.

### 7. Grant paid plans

```
/allow USER_ID STANDARD
/allow USER_ID PRO 60
/deny USER_ID
```

---

## Directory layout

```
/opt/line-searcher/
├── app/                 # Application code
│   ├── bot/             # Handlers, keyboards, middlewares
│   ├── core/            # Config, models, database
│   ├── storage/         # Catalog, downloader
│   ├── search/          # Streaming search engine
│   ├── jobs/            # Queue & workers
│   ├── broadcast/       # Broadcast worker
│   └── main.py
├── data/
│   ├── users/users.json
│   ├── catalog/
│   ├── jobs/
│   ├── results/<user_id>/<job_id>/
│   ├── temp/
│   ├── plans.json
│   ├── settings.json
│   └── storage_config.json
├── logs/
├── deploy/
├── .env
└── requirements.txt
```

---

## Admin commands

| Command | Description |
|---------|-------------|
| `/admin` | Open Admin Panel |
| `/allow USER_ID PLAN [DAYS]` | Grant / extend plan |
| `/deny USER_ID` | Block user |
| `/user USER_ID` | User details |
| `/users` | Recent users |
| `/export` | Download `users.json` |
| `/stats` | Quick stats |
| `/jobs` | (via panel) |
| `/cancel JOB-ID` | Cancel any job |
| `/maintenance on\|off` | Toggle maintenance |
| `/refresh` | Refresh public catalog |

---

## Search behavior (preserved)

- Line-by-line streaming
- Case-insensitive by default; optional case-sensitive
- Multiple keywords → single pass per file
- Exact original matching lines only (no filenames, line numbers, prefixes)
- Automatic duplicate removal (hash-based)
- No empty result files
- Result splitting under Telegram size limits
- Encoding fallbacks: UTF-8, UTF-8-SIG, CP1252, Latin-1, ASCII
- One bad file does not stop the job

---

## Acceptance checklist

| Requirement | Status |
|-------------|--------|
| Automatic Free registration (no `/allow`) | ✅ IMPLEMENTED |
| Free daily limit + reset | ✅ IMPLEMENTED |
| Paid plans + expiry → FREE | ✅ IMPLEMENTED |
| Upgrade request → contact admin (no auto-pay) | ✅ IMPLEMENTED |
| Public Free Storage config + name + ID | ✅ IMPLEMENTED |
| Private storage mapping field | ✅ IMPLEMENTED |
| Streaming multi-GB search | ✅ IMPLEMENTED |
| Multi-keyword, dedup, exact lines | ✅ IMPLEMENTED |
| Job states, queue, concurrency | ✅ IMPLEMENTED |
| Live progress (single editable message) | ✅ IMPLEMENTED |
| Byte progress + real ETA | ✅ IMPLEMENTED |
| User / admin cancel | ✅ IMPLEMENTED |
| Restart recovery | ✅ IMPLEMENTED |
| Result retention + cleanup | ✅ IMPLEMENTED |
| Admin panel | ✅ IMPLEMENTED |
| Broadcast (filters, preview, rate limit) | ✅ IMPLEMENTED |
| Maintenance mode | ✅ IMPLEMENTED |
| Secrets in env only | ✅ IMPLEMENTED |
| Local Bot API integration | ✅ IMPLEMENTED |
| Full historical channel scan via Bot API only | ⚠️ REQUIRES CONFIGURATION / optional MTProto indexer (Bot API limitation) |
| systemd deployment units | ✅ IMPLEMENTED |

---

## Security notes

- Admin access by Telegram user ID only
- Secrets never logged or sent to users
- User / job / storage isolation
- Safe filenames; path traversal blocked
- Rate limiting on broadcasts
- No personal Telegram credentials requested from users

---

## License / support

Commercial-style internal tool. Support: **@Arehsmanbot**
