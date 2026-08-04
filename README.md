# school-portal-agent

Proactive Telegram assistant for parents whose kids' school runs on **MyClassBoard (MCB)**.

Instead of logging into the portal, the agent scrapes it for you and pushes a formatted digest to your family Telegram group four times a day:

| Time (local) | Slot | Content |
|---|---|---|
| 06:30 | Morning | Full digest — today's announcements, today's homework, upcoming events, yesterday's LLM note (recap) |
| 16:15 (Mon–Fri) | Homework | Just what the class diary posted today. No accumulation. |
| 18:00 | Reminders | Nudges for anything with an explicit deadline **5, 3, 2, or 1 day out** |
| 19:00 | Evening | Wrap-up with an LLM-generated "note of the day" (2–3 sentences) |

Plus an interactive bot (`/today`, `/homework`, `/events`, `/reload`, or plain-English questions) that answers on demand.

Attachments (PDF / DOCX) are downloaded, text-extracted, and summarised so the deadline hidden on page 2 of a circular still shows up as a reminder — with a direct link back to the source doc.

---

## Requirements

- A **MyClassBoard parent-portal login** for each child.
- **Python 3.10+** (developed on 3.12).
- A **Telegram account** (to register a bot via [@BotFather](https://t.me/BotFather) and add it to your family chat).
- An **Azure OpenAI** deployment (or edit `mcb_digest/llm.py` to point at OpenAI, Anthropic, a local model, etc. — the interface is small).
- **Windows** with Task Scheduler for the default schedule, or Linux/macOS with `cron` / `systemd` (equivalent one-liners below).

---

## Quickstart

```powershell
# 1. Clone
git clone https://github.com/sudheewayne1972/school-portal-agent.git
cd school-portal-agent

# 2. Python deps
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # on Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# 3. Config
Copy-Item mcb_config.example.ini mcb_config.ini
Copy-Item .env.example .env
# Now edit both files with your real values (see below).

# 4. Register the Telegram bot
#    - DM @BotFather on Telegram, `/newbot`, follow prompts, copy the token
#      into TELEGRAM_BOT_TOKEN in .env.
#    - Create a family group, add your bot, send /start there.
#      The bot prints the numeric chat_id to the log — copy it into
#      TELEGRAM_CHAT_ID in .env.

# 5. Smoke test (scrape + build digest, don't post)
python run_digest.py --dry-run
python push_digest.py --slot morning --dry-run

# 6. Real run (posts to your group)
python push_digest.py --slot morning

# 7. Schedule it (Windows)
powershell -ExecutionPolicy Bypass -File .\setup_scheduler.ps1
powershell -ExecutionPolicy Bypass -File .\setup_homework_push_scheduler.ps1
powershell -ExecutionPolicy Bypass -File .\setup_reminders_scheduler.ps1
powershell -ExecutionPolicy Bypass -File .\setup_diary_scheduler.ps1
powershell -ExecutionPolicy Bypass -File .\setup_bot_autostart.ps1
```

The interactive bot runs separately (once started, it stays running):

```powershell
python run_telegram_bot.py
```

---

## Config files

### `mcb_config.ini`

One `[ChildN]` section per student:

```ini
[Global]
log_file = mcb_scheduler.log

[Child1]
name = FirstChild
username = <portal username>
password = <portal password>

[Child2]
name = SecondChild
username = <portal username>
password = <portal password>
```

### `.env`

See [.env.example](.env.example) for the full list. Minimum required:

- `TELEGRAM_BOT_TOKEN` — from @BotFather
- `TELEGRAM_CHAT_ID` — the group / DM to post to
- `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_KEY`, `AZURE_OPENAI_DEPLOYMENT` — for the LLM note-of-day, attachment summaries, and Q&A

Optional: SMTP settings if you also want email delivery.

---

## How it's wired

```
                    ┌────────────────────────────────────────────┐
                    │           Windows Task Scheduler           │
                    └───────────────┬────────────────────────────┘
                                    │
   ┌────────────────────┬───────────┼───────────────┬───────────────┐
   │ 06:30              │ 16:15     │ 18:00         │ 19:00         │
   │ full digest        │ homework  │ reminders     │ evening       │
   ▼                    ▼           ▼               ▼               │
 run_digest.py     run_diary.py   push_reminders   run_digest.py    │
   │  (Playwright                 .py               │               │
   │   scrape)                                      │               │
   ▼                                                ▼               │
 push_digest.py --slot morning                push_digest.py --slot │
                                                    evening         │
                                                                    │
        Everything writes to a Telegram group via the Bot API.      │
                                                                    │
 run_telegram_bot.py (long-running)  — interactive Q&A on the same  │
                                       group / DM.                  │
                                                                    │
```

Under the hood:

- [mcb_digest/scraper.py](mcb_digest/scraper.py) — Playwright login + announcements HTML fetch. **This module is portal-specific to MyClassBoard.**
- [mcb_digest/diary_scraper.py](mcb_digest/diary_scraper.py) — Playwright fetch of the class-diary / homework calendar. Also portal-specific.
- [mcb_digest/parser.py](mcb_digest/parser.py) — BeautifulSoup parse + attachment download + PDF/DOCX text extraction.
- [mcb_digest/attachments.py](mcb_digest/attachments.py) — LLM summarisation of attachment text (cached on-disk; only new attachments incur token cost).
- [mcb_digest/extractor.py](mcb_digest/extractor.py) — regex date extraction across title / body / attachment text → `Event` records.
- [mcb_digest/reminders.py](mcb_digest/reminders.py) — persistent event store (`data/events.json`) + 5/3/2/1-day reminder computation.
- [mcb_digest/telegram_digest.py](mcb_digest/telegram_digest.py) — the shared Telegram-HTML digest builder used by all four pushes and the bot.
- [mcb_digest/channels/telegram_bot.py](mcb_digest/channels/telegram_bot.py) — the interactive polling bot.
- [mcb_digest/agent.py](mcb_digest/agent.py) + [mcb_digest/llm.py](mcb_digest/llm.py) — LLM plumbing.

---

## Customising

- **Change push times** — edit the `-At` values in the `setup_*.ps1` scripts and re-run them. They unregister and re-create the tasks.
- **Disable a slot** — unregister the corresponding task: `Unregister-ScheduledTask -TaskName 'MCB Data Refresh'`.
- **Add or remove a child** — edit `mcb_config.ini`.
- **Swap LLM providers** — replace the `LLM` class in [mcb_digest/llm.py](mcb_digest/llm.py). The rest of the code only calls `LLM().chat(messages, max_output_tokens=..., temperature=...)`.
- **Change the reminder cadence** — set `reminder_offsets_days` in [mcb_digest/config.py](mcb_digest/config.py) (default `(5, 3, 2, 1)`).
- **New academic year** — set `DATA_FLOOR_DATE=YYYY-MM-DD` in `.env`, then run `python purge_stale.py` to drop events older than the floor.

---

## Not on MyClassBoard?

Most of the pipeline is portal-agnostic. Only [mcb_digest/scraper.py](mcb_digest/scraper.py) and [mcb_digest/diary_scraper.py](mcb_digest/diary_scraper.py) know how to log in to CHIREC's MCB tenant. Contributions welcome:

1. Add `mcb_digest/scraper_<portal>.py` and `mcb_digest/diary_scraper_<portal>.py` returning HTML in the same shape.
2. Extend [mcb_digest/parser.py](mcb_digest/parser.py) if the announcement card markup differs.
3. Everything downstream (event extraction, reminders, LLM notes, Telegram formatting, bot) works unchanged.

---

## Linux / macOS

Replace the `setup_*.ps1` scripts with `cron` lines (or systemd timers):

```cron
30 6  * * *   cd /path/to/school-portal-agent && python run_digest.py --dry-run && python push_digest.py --slot morning
15 16 * * 1-5 cd /path/to/school-portal-agent && python run_diary.py
0  18 * * *   cd /path/to/school-portal-agent && python push_reminders.py
0  19 * * *   cd /path/to/school-portal-agent && python run_digest.py --dry-run && python push_digest.py --slot evening
```

Run `python run_telegram_bot.py` under `systemd` or `screen`/`tmux` for the interactive bot.

---

## Security

- **Never** commit `.env` or `mcb_config.ini`. Both are in [.gitignore](.gitignore).
- Rotate the Telegram bot token and Azure OpenAI key if they ever appear in a tracked file.
- The scraper stores your portal credentials in plain text in `mcb_config.ini`. Treat that file like a password vault (file permissions, disk encryption).
- Attachment PDFs are cached under `data/*_attachments/`. Wipe these if you loan out the machine.

---

## License

[MIT](LICENSE).
