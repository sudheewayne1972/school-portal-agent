"""Telegram bot channel for the MCB Agent.

Runs a polling bot that:
  * Responds to /help, /today, /events, /reload, /reset commands
    * Answers messages in configured chats and DMs
  * Pushes the daily digest + reminders to a configured group chat

Environment (read via mcb_digest.config._load_env_file):
  TELEGRAM_BOT_TOKEN     Token from @BotFather (required)
  TELEGRAM_CHAT_ID       Optional numeric chat_id for scheduled pushes
  TELEGRAM_ALLOWED_CHATS Optional comma-separated allow-list of chat_ids
                          (if set, bot ignores messages from anywhere else)
"""
from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

from telegram import BotCommand, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.request import HTTPXRequest

from mcb_digest.agent import Agent
from mcb_digest.config import load_config
from mcb_digest.diary_store import entries_on, recent_entries
from mcb_digest.extractor import _extract_dates_diary, set_data_floor
from mcb_digest.reminders import load_all_events, upcoming_events
from mcb_digest.telegram_digest import (
    build_html_digest,
    chunk_for_telegram,
    esc as _esc,
    pretty_date as _pretty_date,
)

log = logging.getLogger("mcb_digest.telegram")

IST = ZoneInfo("Asia/Kolkata")


HELP_TEXT = (
    "<b>School Portal Agent</b>\n"
    "I answer questions about your kids' school announcements &amp; homework.\n\n"
    "<b>Commands</b>\n"
    "/today — today's summary (reminders, new posts, homework, upcoming)\n"
    "/homework — homework &amp; class-diary for today (and last 7 days)\n"
    "/events — upcoming events, grouped by date\n"
    "/reload — re-read the latest scraped data\n"
    "/reset — clear chat history\n"
    "/help — show this message\n\n"
    "In the family group, just type your question.\n"
    "In a DM, just type your question."
)


# ---------- markdown → HTML for LLM output ----------

_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_ITAL = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_MD_HEAD = re.compile(r"^\s*#{1,6}\s+(.*)$", re.MULTILINE)
_MD_BULLET = re.compile(r"^\s*[-*]\s+", re.MULTILINE)


def _markdown_to_html(text: str) -> str:
    """Best-effort convert common Markdown from the LLM into Telegram HTML.
    Escapes HTML first so the model can't inject tags."""
    if not text:
        return ""
    out = _esc(text)
    out = _MD_HEAD.sub(r"<b>\1</b>", out)
    out = _MD_BOLD.sub(r"<b>\1</b>", out)
    out = _MD_ITAL.sub(r"<i>\1</i>", out)
    out = _MD_BULLET.sub("• ", out)
    return out


def _allowed(update: Update, allowed_chats: Optional[set[int]]) -> bool:
    if not allowed_chats:
        return True
    chat = update.effective_chat
    allowed = chat is not None and chat.id in allowed_chats
    if not allowed:
        log.warning("Ignoring update %s from unauthorized chat_id=%s",
                    update.update_id, chat.id if chat else None)
    return allowed


async def _log_update(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    message = update.effective_message
    log.info("Received update=%s chat_id=%s type=%s message=%s command=%s",
             update.update_id,
             chat.id if chat else None,
             chat.type if chat else None,
             bool(message),
             bool(message and message.text and message.text.startswith("/")))


async def _reply_typing(update: Update) -> None:
    if update.effective_chat:
        try:
            await update.effective_chat.send_action(ChatAction.TYPING)
        except Exception:
            pass


async def _register_commands(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("today", "Today's summary"),
        BotCommand("homework", "Today's homework and active deadlines"),
        BotCommand("events", "Upcoming events"),
        BotCommand("reload", "Reload the latest scraped data"),
        BotCommand("reset", "Clear chat history"),
        BotCommand("help", "Show available commands"),
    ])


# ---------- command handlers ----------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    allowed = ctx.application.bot_data.get("allowed_chats")
    if not _allowed(update, allowed):
        return
    chat = update.effective_chat
    chat_info = (
        f"chat_id: <code>{chat.id}</code> · type: <code>{chat.type}</code>"
        if chat else ""
    )
    await update.message.reply_text(
        f"👋 Hi! I'm the MCB Agent.\n{chat_info}\n\n{HELP_TEXT}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update, ctx.application.bot_data.get("allowed_chats")):
        return
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update, ctx.application.bot_data.get("allowed_chats")):
        return
    await _reply_typing(update)
    html = _build_today_html(ctx)
    await _send_long(update.effective_chat.id, ctx, html, parse_mode=ParseMode.HTML)


async def cmd_events(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update, ctx.application.bot_data.get("allowed_chats")):
        return
    await _reply_typing(update)
    today = datetime.now(IST).date()
    ups = upcoming_events(load_all_events(), today=today)
    if not ups:
        await update.message.reply_text("No upcoming events tracked.")
        return

    # Group by date
    by_date: dict[str, list[dict]] = {}
    for ev in ups[:60]:
        by_date.setdefault(ev.get("event_date", "?"), []).append(ev)

    lines: List[str] = ["🗓 <b>Upcoming events</b>"]
    for ed in sorted(by_date):
        lines.append(f"\n<b>{_esc(_pretty_date(ed, today))}</b>")
        for ev in by_date[ed]:
            child = _esc(ev.get("child", ""))
            title = _esc(ev.get("title", ""))
            kind_glyph = "⏰" if ev.get("kind") == "deadline" else "•"
            lines.append(f"  {kind_glyph} <b>{child}</b>: {title}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_homework(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update, ctx.application.bot_data.get("allowed_chats")):
        return
    await _reply_typing(update)
    agent: Agent = ctx.application.bot_data["agent"]
    set_data_floor(agent.cfg.data_floor_date)
    today = datetime.now(IST).date()
    today_iso = today.isoformat()

    lines: List[str] = [f"📔 <b>Homework — {today.strftime('%a, %d %b %Y')}</b>"]
    any_content = False
    for child in agent.cfg.children:
        todays = entries_on(child.name, today_iso)
        recent = recent_entries(child.name, days=7, today=today)
        # Older entries are only shown if they have an unexpired deadline
        older_active: list = []
        for e in recent:
            if e.diary_date == today_iso:
                continue
            dates = _extract_dates_diary(e.description or "")
            if any(d >= today for d in dates):
                older_active.append((e, [d for d in dates if d >= today]))

        lines.append(f"\n<b>{_esc(child.name)}</b> — {len(todays)} today · "
                     f"{len(older_active)} with active deadline")
        if todays:
            any_content = True
            by_subj: dict[str, list] = {}
            for e in todays:
                by_subj.setdefault(e.subject, []).append(e)
            for subj, es in sorted(by_subj.items()):
                lines.append(f"  <b>{_esc(subj)}</b>")
                for e in es:
                    desc = " ".join(e.description.split())
                    if len(desc) > 260:
                        desc = desc[:260].rstrip() + "…"
                    teacher = f" — <i>{_esc(e.teacher)}</i>" if e.teacher else ""
                    lines.append(f"    • <i>{_esc(e.kind)}</i>: {_esc(desc)}{teacher}")
                    # Inline deadline callout
                    active = [d for d in _extract_dates_diary(e.description or "")
                              if d >= today]
                    if active:
                        due = min(active)
                        lines.append(
                            f"      ⏰ <b>due {_esc(_pretty_date(due.isoformat(), today))}</b>"
                        )
        else:
            lines.append("  <i>Nothing posted today.</i>")

        if older_active:
            any_content = True
            lines.append("  <i>Still pending (from earlier):</i>")
            for e, active_dates in older_active[:10]:
                desc = " ".join(e.description.split())
                if len(desc) > 180:
                    desc = desc[:180].rstrip() + "…"
                due = min(active_dates)
                due_str = _pretty_date(due.isoformat(), today)
                lines.append(
                    f"    ⏰ due <b>{_esc(due_str)}</b> · "
                    f"<b>{_esc(e.subject)}</b> <i>{_esc(e.kind)}</i>: {_esc(desc)}"
                )

    if not any_content:
        lines.append("\n<i>No diary entries on disk. Try /reload after 4:30 PM.</i>")
    await _send_long(update.effective_chat.id, ctx, "\n".join(lines),
                     parse_mode=ParseMode.HTML)


async def cmd_reload(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update, ctx.application.bot_data.get("allowed_chats")):
        return
    agent: Agent = ctx.application.bot_data["agent"]
    agent.reload_context()
    ann_parts = [f"{name}: {len(anns)}" for name, anns in agent.ctx.per_child.items()]
    dia_parts = [f"{name}: {len(des)}"
                 for name, des in (agent.ctx.per_child_diary or {}).items()]
    await update.message.reply_text(
        "🔄 Reloaded.\nAnnouncements — " + ", ".join(ann_parts)
        + "\nDiary (7d) — " + ", ".join(dia_parts)
    )


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update, ctx.application.bot_data.get("allowed_chats")):
        return
    agent: Agent = ctx.application.bot_data["agent"]
    agent.reset()
    await update.message.reply_text("🧹 Chat history cleared.")


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update, ctx.application.bot_data.get("allowed_chats")):
        return
    msg = update.effective_message
    if not msg or not msg.text:
        return
    chat = update.effective_chat
    bot_username = (await ctx.bot.get_me()).username

    question = re.sub(f"@{re.escape(bot_username)}", "", msg.text,
                      flags=re.IGNORECASE).strip()
    if not question:
        return

    await _reply_typing(update)
    agent: Agent = ctx.application.bot_data["agent"]
    requester = msg.from_user.first_name if msg.from_user else None
    try:
        answer = agent.ask(question, requester=requester)
    except Exception as e:
        log.exception("agent.ask failed")
        answer = f"Sorry, I hit an error: {e}"

    html = _markdown_to_html(answer)
    await _send_long(chat.id, ctx, html, parse_mode=ParseMode.HTML)


# ---------- digest & transport ----------

def _build_today_html(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    """Build a Today digest, in Telegram HTML, from the on-disk data (no scrape)."""
    agent: Agent = ctx.application.bot_data["agent"]
    return build_html_digest(agent)


async def _send_long(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, text: str,
                     parse_mode: Optional[str] = None) -> None:
    """Telegram messages max out at 4096 chars; chunk on line boundaries."""
    for c in chunk_for_telegram(text):
        await ctx.bot.send_message(chat_id=chat_id, text=c, parse_mode=parse_mode)


def build_application(token: str, agent: Optional[Agent] = None) -> Application:
    agent = agent or Agent()

    # Generous timeouts so a slow first TLS handshake doesn't kill bootstrap.
    request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0, write_timeout=30.0, pool_timeout=30.0)
    get_updates_request = HTTPXRequest(connect_timeout=30.0, read_timeout=40.0, write_timeout=30.0, pool_timeout=30.0)
    app = (
        ApplicationBuilder()
        .token(token)
        .request(request)
        .get_updates_request(get_updates_request)
        .post_init(_register_commands)
        .build()
    )
    app.bot_data["agent"] = agent

    allowed = os.environ.get("TELEGRAM_ALLOWED_CHATS", "").strip()
    if allowed:
        app.bot_data["allowed_chats"] = {int(x) for x in allowed.split(",") if x.strip()}
    else:
        app.bot_data["allowed_chats"] = None

    # Scheduled daily pushes are driven by push_digest.py via Windows Task
    # Scheduler (see setup_scheduler.ps1). The bot process only handles
    # interactive Q&A. TELEGRAM_CHAT_ID is kept in bot_data so /today etc.
    # can reference the target chat if needed later.
    target = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if target:
        app.bot_data["target_chat_id"] = int(target)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("homework", cmd_homework))
    app.add_handler(CommandHandler("events", cmd_events))
    app.add_handler(CommandHandler("reload", cmd_reload))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(TypeHandler(Update, _log_update), group=-1)
    return app


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    load_config()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
        return 1

    app = build_application(token)
    log.info("Starting polling. Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
    return 0


if __name__ == "__main__":
    sys.exit(main())


