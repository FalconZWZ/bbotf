# Birthday Reminder Bot - Agent Guide

## Project Overview

A Telegram bot that helps users track birthdays and receive configurable reminders. Users register birthdays (with or without birth year), choose when to be reminded (0, 1, 3, or 7 days before), and receive automatic notifications during daytime hours. Supports English and Russian.

**Stack**: Python 3.10, pyTelegramBotAPI, SQLite (WAL mode), Docker  
**Repository**: https://github.com/Nikfilk2030/birthday_reminder_bot

---

## Architecture

```
bot.py  ──imports──>  db.py  ──imports──>  utils.py
  │                     ^                      │
  ├──imports──>  i18n.py ───imports──> db.py   │
  ├──imports──>  utils.py                      │
  └──imports──>  telebot (pyTelegramBotAPI)    │
                                               │
                 i18n.py  <──lazy import───────┘
```

- **bot.py** (1186 lines) — Main entry point. Telegram command/callback handlers, 3 background threads (birthday reminders, backup pings, log cleanup), user state machine.
- **db.py** (590 lines) — SQLite database layer. All CRUD operations, reminder flag management, schema initialization.
- **utils.py** (379 lines) — Date parsing, input validation, time duration parsing, age metrics, message splitting.
- **i18n.py** (179 lines) — Internationalization. Loads translations from `translations.json`, resolves per-user language preferences.
- **backup_db.py** (83 lines) — Standalone DB backup utility (file copy + SQLite backup + SQL dump).
- **tests.py** (1277 lines) — Unit tests for all modules.

### Circular dependency note
`utils.py` uses a lazy import (`get_i18n()`) to avoid circular dependency with `i18n.py`, which imports `db.py`, which imports `utils.py`.

---

## File Map

| File | Purpose |
|---|---|
| `bot.py` | Main bot logic, command handlers, background threads, entry point |
| `db.py` | SQLite database operations, schema, TBirthday/TBackupPingSettings classes |
| `utils.py` | Date parsing, validation, time parsing, age metrics, message splitting |
| `i18n.py` | Internationalization module, translation loading, language management |
| `backup_db.py` | Database backup/restore utility (standalone script) |
| `tests.py` | Unit tests (13 test classes) |
| `translations.json` | All UI strings in English and Russian |
| `requirements.txt` | Dependencies: pyTelegramBotAPI>=4.18.0, python-dotenv>=1.0.0 |
| `Dockerfile` | Python 3.10-slim, runs tests then bot |
| `docker-compose.yml` | Service config, volume mounts for data.db |
| `start.sh` | Launch script with --prestable, --production, --no-docker options |
| `.flake8` | Linter config (max-line-length 200) |
| `birthdays` | Legacy placeholder file (unused, data lives in SQLite) |
| `LICENSE` | MIT |
| `README.md` | User-facing documentation |

---

## Database Schema

SQLite database file: `data.db` (production) or `data_prestable.db` (prestable).  
WAL journal mode enabled at init.

### Table: `birthdays`
| Column | Type | Description |
|---|---|---|
| id | INTEGER PK AUTOINCREMENT | Unique birthday ID |
| chat_id | INTEGER NOT NULL | Telegram chat ID of the owner |
| name | TEXT NOT NULL | Person's name |
| birthday | DATE NOT NULL | Stored as "YYYY-MM-DD". Year=1900 means no year provided |
| has_year | BOOLEAN | Whether the user provided a birth year |
| was_reminded_0_days_ago | BOOLEAN | Flag: "today" reminder already sent |
| was_reminded_1_days_ago | BOOLEAN | Flag: "1 day before" reminder already sent |
| was_reminded_3_days_ago | BOOLEAN | Flag: "3 days before" reminder already sent |
| was_reminded_7_days_ago | BOOLEAN | Flag: "7 days before" reminder already sent |

### Table: `user_reminder_settings`
| Column | Type | Description |
|---|---|---|
| chat_id | INTEGER PK | Telegram chat ID |
| reminder_days | TEXT | Comma-separated enabled days, e.g. "0,1,3,7" |
| created_at | TIMESTAMP | Row creation time |

### Table: `user_language_settings`
| Column | Type | Description |
|---|---|---|
| chat_id | INTEGER PK | Telegram chat ID |
| language_code | TEXT | "en" or "ru" |
| created_at | TIMESTAMP | Row creation time |
| updated_at | TIMESTAMP | Last language change |

### Table: `backup_ping_settings`
| Column | Type | Description |
|---|---|---|
| chat_id | INTEGER PK | Telegram chat ID |
| last_updated_timestamp | TIMESTAMP | When the last backup was sent |
| update_timedelta | INT | Backup interval in minutes |
| is_active | BOOLEAN | Whether periodic backups are enabled |

---

## Key Flows

### Startup (`bot.py:1157-1186`)
1. `db.init_db()` — creates tables if they don't exist, enables WAL
2. Start 3 daemon threads:
   - `process_backup_pings()` — checks backup schedules every 5 min
   - `process_birthday_pings()` — checks upcoming birthdays every 5 min
   - `log_cleaner()` — deletes log files older than 30 days, runs every 24h
3. `bot.polling(none_stop=True, timeout=60)` — long-polling loop

### Birthday Registration (`bot.py:748-758`, `bot.py:1092-1133`)
1. User clicks "Register Birthday" or sends `/register_birthday`
2. Bot sets `user_states[chat_id] = AwaitingBirthday`, sends instructions
3. User sends message with name/date pairs (one name per line, then date on next line)
4. `utils.validate_birthday_input()` checks format
5. `utils.parse_dates()` parses into `(name, datetime, has_year)` tuples
6. For each: `db.register_birthday(chat_id, name, datetime, has_year)`
7. Confirmation sent, state reset to Default, instruction messages deleted

**Date formats accepted**:
- `DD.MM.YYYY` — full date (e.g., `5.06.2001`)
- `DD.MM` — without year (stored with year=1900)
- `DD.MM AGE` — calculates birth year from current age

### Birthday Reminder Loop (`bot.py:667-737`)
Every 5 minutes:
1. Sleep 5 minutes
2. Check `is_daytime()` (7 AM - 8 PM local time) — skip if night
3. `db.reset_birthday_reminder_flags()` — reset all 4 reminder flags for birthdays whose `%m-%d` is >10 days from today
4. For each reminder window in `[0, 1, 3, 7]` days:
   a. `db.get_upcoming_birthdays(days)` — SQL matches `strftime('%m-%d')` AND `was_reminded_X = FALSE`
   b. For each match: check user's `reminder_settings` includes this day
   c. Send message (with age if `has_year`)
   d. `db.mark_birthday_reminder_sent(id, days_until)` — set flag to TRUE
   e. On 403 (bot blocked): mark as sent to prevent retries

### Backup Ping Loop (`bot.py:761-797`)
Every 5 minutes:
1. Fetch all chat_ids from `user_reminder_settings`
2. For each: load `backup_ping_settings`, skip if `is_active = FALSE`
3. Check if enough time has elapsed since `last_updated_timestamp`
4. If due: `db.update_backup_ping()`, send formatted birthday list

### Reminder Flag Reset Logic (`db.py:465-518`)
Flags (`was_reminded_X_days_ago`) prevent duplicate reminders. They are reset (set to FALSE) when the birthday's `%m-%d` is:
- More than 10 days in the past, AND
- More than 10 days in the future

This ensures flags are cleared well after the birthday passes, enabling reminders to work again next year. Only rows with at least one flag set to TRUE are updated (optimization).

---

## User State Machine

States defined in `TUserState` enum (`bot.py:59-63`):

```
Default ──/register_birthday──> AwaitingBirthday ──(valid input)──> Default
Default ──/register_backup────> AwaitingInterval ──(valid input)──> Default
Default ──/delete_birthday────> AwaitingDeletion ──(valid input)──> Default
```

State stored in `user_states: dict[int, TUserState]` (in-memory, lost on restart).

---

## Configuration

### Environment Variables (`.env` file)
| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes (production) | Production bot token |
| `PRESTABLE_TELEGRAM_BOT_TOKEN` | Yes (prestable) | Test bot token |
| `PRESTABLE_MODE` | No | Set to "true" for prestable mode |
| `DB_FILE` | No | Override database filename (default: `data.db`) |

### Prestable Mode
When `PRESTABLE_MODE=true`:
- Uses `PRESTABLE_TELEGRAM_BOT_TOKEN` instead of `TELEGRAM_BOT_TOKEN`
- Database file switches to `data_prestable.db`
- Allows safe testing against a separate bot and database

---

## Deployment

### Docker (recommended)
```bash
# Production
docker compose up --build -d

# Prestable
PRESTABLE_MODE=true docker compose up --build -d
```

Volumes mount `data.db` and `backup_ping_settings.db` for persistence.

### Direct Python
```bash
pip install -r requirements.txt
python bot.py
```

### start.sh Options
```bash
./start.sh                          # Production + Docker
./start.sh --prestable              # Prestable + Docker
./start.sh --no-docker              # Production, direct Python
./start.sh --prestable --no-docker  # Prestable, direct Python
```

The script creates a database backup before launching and runs code formatters (black, isort, flake8) in production mode.

---

## Testing

```bash
python -m unittest discover    # Run all tests
python -m unittest tests       # Same thing
```

Tests use an in-memory SQLite database (`:memory:`). No network calls — bot API is not mocked, only DB and utility functions are tested.

### Test Classes (tests.py)
| Class | What it tests |
|---|---|
| `TestTimestampParser` | Duration string parsing ("5 hours", "3 дня") |
| `TestParseDate` | Date format parsing, edge cases, leap years |
| `TestValidateBirthdayInput` | Input format validation and error messages |
| `TestDatabase` | Birthday CRUD, reminder settings, backup pings |
| `TestUtils` | Helper functions, log cleanup |
| `TestBackupPingSettings` | Backup ping data structures |
| `TestBirthdayAgeCalculation` | Age computation with leap year edge cases |
| `TestMultipleBirthdayRegistration` | Bulk birthday registration |
| `TestMultipleBirthdayDeletion` | Bulk deletion |
| `TestBirthdayReminderLogic` | Reminder flags, duplicate prevention |
| `TestInternationalization` | Translation loading, language switching |
| `TestComputeAgeMetrics` | Age statistics (avg, min, max, median) |
| `TestFindMostPopularDate` | Date popularity analysis |

---

## Important Implementation Details

### Leap Year Handling
`db._safe_replace_year()` handles Feb 29 birthdays in non-leap years by falling back to Feb 28. Used in `TBirthday.__str__()` for age calculation. **Note**: `process_birthday_pings()` at `bot.py:689` uses `birthday.replace(year=...)` directly without this helper — this is a known bug.

### Birthdays Without Year
Stored with `year=1900` (`utils.DEFAULT_BD_YEAR`). The `has_year` boolean controls whether age is displayed.

### Daytime-Only Notifications
`utils.is_daytime()` returns `True` between 7 AM and 8 PM (inclusive) using server local time. No per-user timezone support.

### Telegram Message Limits
`utils.split_message()` splits messages at line boundaries to stay within Telegram's 4096-character limit.

### Logging
Dual output: `bot.log` file + stderr. Log format includes timestamp, level, message, filename, and line number. Old log files (>30 days) are cleaned by the `log_cleaner` thread.

### Telegram Stars Payments
Support/donation feature using Telegram Stars (XTR currency). Amounts: 50, 100, 250, 500, 1000 stars. Pre-checkout always approved.

### i18n System
- Translations in `translations.json` with dot-notation keys (e.g., `messages.welcome_title`)
- Per-user language stored in `user_language_settings` table
- Fallback to English if translation missing
- `{variable}` substitution via Python `.format()`
