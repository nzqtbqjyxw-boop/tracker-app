import asyncio
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

def get_required_setting(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} environment variable is required")
    return value


BOT_TOKEN = get_required_setting("BOT_TOKEN")
WEBAPP_URL = get_required_setting("WEBAPP_URL")
PORT = int(os.getenv("PORT", "8080"))

DB_PATH = Path(__file__).parent / "tracker.db"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tracker-bot")

# ---------------------------------------------------------------------------
# База данных (SQLite)
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS habits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            icon TEXT,
            priority TEXT DEFAULT 'medium',
            sort_order INTEGER DEFAULT 0,
            goal TEXT DEFAULT 'daily',
            freq INTEGER DEFAULT 3,
            color TEXT,
            reminder_enabled INTEGER DEFAULT 0,
            reminder_time TEXT,
            reminder_text TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS habit_logs (
            habit_id INTEGER NOT NULL,
            log_date TEXT NOT NULL,
            PRIMARY KEY (habit_id, log_date)
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            amount REAL NOT NULL,
            category TEXT,
            icon TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS budgets (
            chat_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            limit_amount REAL NOT NULL,
            PRIMARY KEY (chat_id, category)
        );
        """
    )
    conn.commit()
    conn.close()


def ensure_user(chat_id: int) -> None:
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO users (chat_id, created_at) VALUES (?, ?)",
        (chat_id, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Telegram-бот
# ---------------------------------------------------------------------------

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()


@dp.message(CommandStart())
async def start_handler(message: Message) -> None:
    ensure_user(message.chat.id)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Открыть трекер",
                    web_app=WebAppInfo(url=f"{WEBAPP_URL}?uid={message.chat.id}"),
                )
            ]
        ]
    )
    await message.answer(
        "Добро пожаловать! Откройте трекер привычек и финансов:",
        reply_markup=keyboard,
    )


# ---------------------------------------------------------------------------
# Планировщик напоминаний
# ---------------------------------------------------------------------------

def reschedule_reminders_for_habit(habit_id, chat_id, enabled, time_str, text, habit_name) -> None:
    job_id = f"reminder_{habit_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    if not enabled or not time_str:
        return
    try:
        hour, minute = map(int, time_str.split(":"))
    except (ValueError, AttributeError):
        return
    reminder_text = text or f"Не забудь: {habit_name}"

    async def send_reminder(chat_id=chat_id, reminder_text=reminder_text):
        try:
            await bot.send_message(chat_id, f"🔔 {reminder_text}")
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось отправить напоминание %s: %s", chat_id, exc)

    scheduler.add_job(
        send_reminder,
        trigger=CronTrigger(hour=hour, minute=minute),
        id=job_id,
        replace_existing=True,
    )


def load_all_reminders_into_scheduler() -> None:
    conn = get_db()
    rows = conn.execute(
        "SELECT id, chat_id, name, reminder_enabled, reminder_time, reminder_text FROM habits"
    ).fetchall()
    conn.close()
    for row in rows:
        reschedule_reminders_for_habit(
            row["id"], row["chat_id"], bool(row["reminder_enabled"]),
            row["reminder_time"], row["reminder_text"], row["name"],
        )


# ---------------------------------------------------------------------------
# REST API для мини-аппа
# ---------------------------------------------------------------------------

def cors_response(data, status=200):
    return web.json_response(
        data,
        status=status,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
        },
    )


async def handle_options(request: web.Request) -> web.Response:
    return cors_response({})


async def get_state(request: web.Request) -> web.Response:
    chat_id = int(request.query.get("uid", 0))
    if not chat_id:
        return cors_response({"error": "uid required"}, 400)
    ensure_user(chat_id)
    conn = get_db()
    habits = conn.execute(
        "SELECT * FROM habits WHERE chat_id = ? ORDER BY sort_order", (chat_id,)
    ).fetchall()
    result_habits = []
    for h in habits:
        logs = conn.execute(
            "SELECT log_date FROM habit_logs WHERE habit_id = ?", (h["id"],)
        ).fetchall()
        result_habits.append({
            "id": h["id"],
            "name": h["name"],
            "icon": h["icon"],
            "priority": h["priority"],
            "order": h["sort_order"],
            "goal": h["goal"],
            "freq": h["freq"],
            "color": h["color"],
            "reminderEnabled": bool(h["reminder_enabled"]),
            "reminderTime": h["reminder_time"],
            "reminderText": h["reminder_text"],
            "log": {row["log_date"]: True for row in logs},
        })
    transactions = conn.execute(
        "SELECT * FROM transactions WHERE chat_id = ? ORDER BY created_at DESC", (chat_id,)
    ).fetchall()
    result_tx = [
        {
            "id": t["id"], "type": t["type"], "amount": t["amount"],
            "category": t["category"], "icon": t["icon"], "date": t["created_at"],
        }
        for t in transactions
    ]
    budgets_rows = conn.execute(
        "SELECT category, limit_amount FROM budgets WHERE chat_id = ?", (chat_id,)
    ).fetchall()
    budgets = {b["category"]: b["limit_amount"] for b in budgets_rows}
    conn.close()
    return cors_response({"habits": result_habits, "transactions": result_tx, "budgets": budgets})


async def save_habit(request: web.Request) -> web.Response:
    body = await request.json()
    chat_id = int(body["chatId"])
    ensure_user(chat_id)
    conn = get_db()
    habit_id = body.get("id")
    if habit_id:
        conn.execute(
            """UPDATE habits SET name=?, icon=?, priority=?, sort_order=?, goal=?, freq=?,
               color=?, reminder_enabled=?, reminder_time=?, reminder_text=? WHERE id=? AND chat_id=?""",
            (body["name"], body["icon"], body["priority"], body.get("order", 0),
             body["goal"], body.get("freq", 3), body.get("color"),
             int(body.get("reminderEnabled", False)), body.get("reminderTime"),
             body.get("reminderText"), habit_id, chat_id),
        )
    else:
        cur = conn.execute(
            """INSERT INTO habits (chat_id, name, icon, priority, sort_order, goal, freq, color,
               reminder_enabled, reminder_time, reminder_text, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (chat_id, body["name"], body["icon"], body["priority"], body.get("order", 0),
             body["goal"], body.get("freq", 3), body.get("color"),
             int(body.get("reminderEnabled", False)), body.get("reminderTime"),
             body.get("reminderText"), datetime.utcnow().isoformat()),
        )
        habit_id = cur.lastrowid
    conn.commit()
    row = conn.execute("SELECT name FROM habits WHERE id=?", (habit_id,)).fetchone()
    conn.close()
    reschedule_reminders_for_habit(
        habit_id, chat_id, bool(body.get("reminderEnabled", False)),
        body.get("reminderTime"), body.get("reminderText"), row["name"],
    )
    return cors_response({"id": habit_id})


async def delete_habit(request: web.Request) -> web.Response:
    habit_id = int(request.match_info["habit_id"])
    conn = get_db()
    conn.execute("DELETE FROM habits WHERE id=?", (habit_id,))
    conn.execute("DELETE FROM habit_logs WHERE habit_id=?", (habit_id,))
    conn.commit()
    conn.close()
    job_id = f"reminder_{habit_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    return cors_response({"ok": True})


async def toggle_log(request: web.Request) -> web.Response:
    body = await request.json()
    habit_id = int(body["habitId"])
    log_date = body["date"]
    conn = get_db()
    existing = conn.execute(
        "SELECT 1 FROM habit_logs WHERE habit_id=? AND log_date=?", (habit_id, log_date)
    ).fetchone()
    if existing:
        conn.execute("DELETE FROM habit_logs WHERE habit_id=? AND log_date=?", (habit_id, log_date))
        done = False
    else:
        conn.execute("INSERT INTO habit_logs (habit_id, log_date) VALUES (?, ?)", (habit_id, log_date))
        done = True
    conn.commit()
    conn.close()
    return cors_response({"done": done})


async def add_transaction(request: web.Request) -> web.Response:
    body = await request.json()
    chat_id = int(body["chatId"])
    ensure_user(chat_id)
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO transactions (chat_id, type, amount, category, icon, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (chat_id, body["type"], body["amount"], body.get("category"), body.get("icon"),
         datetime.utcnow().isoformat()),
    )
    conn.commit()
    tx_id = cur.lastrowid
    conn.close()
    return cors_response({"id": tx_id})


async def delete_transaction(request: web.Request) -> web.Response:
    tx_id = int(request.match_info["tx_id"])
    conn = get_db()
    conn.execute("DELETE FROM transactions WHERE id=?", (tx_id,))
    conn.commit()
    conn.close()
    return cors_response({"ok": True})


async def save_budget(request: web.Request) -> web.Response:
    body = await request.json()
    chat_id = int(body["chatId"])
    conn = get_db()
    if body.get("limit") is None:
        conn.execute("DELETE FROM budgets WHERE chat_id=? AND category=?", (chat_id, body["category"]))
    else:
        conn.execute(
            """INSERT INTO budgets (chat_id, category, limit_amount) VALUES (?, ?, ?)
               ON CONFLICT(chat_id, category) DO UPDATE SET limit_amount=excluded.limit_amount""",
            (chat_id, body["category"], body["limit"]),
        )
    conn.commit()
    conn.close()
    return cors_response({"ok": True})


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def telegram_webhook(request: web.Request) -> web.Response:
    data = await request.json()
    from aiogram.types import Update
    update = Update.model_validate(data)
    await dp.feed_update(bot, update)
    return web.Response()


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/api/state", get_state)
    app.router.add_options("/api/{tail:.*}", handle_options)
    app.router.add_post("/api/habits", save_habit)
    app.router.add_delete("/api/habits/{habit_id}", delete_habit)
    app.router.add_post("/api/habits/log", toggle_log)
    app.router.add_post("/api/transactions", add_transaction)
    app.router.add_delete("/api/transactions/{tx_id}", delete_transaction)
    app.router.add_post("/api/budgets", save_budget)
    app.router.add_post(f"/webhook/{BOT_TOKEN}", telegram_webhook)
    # Раздаём index.html и любые другие статичные файлы из той же папки,
    # где лежит bot.py — так сайт и API работают на одном порту.
    static_dir = Path(__file__).parent
    app.router.add_get("/", lambda request: web.FileResponse(static_dir / "index.html"))
    app.router.add_static("/", static_dir, show_index=False)
    return app


# ---------------------------------------------------------------------------
# Точка входа: polling локально, webhook в проде (если задан WEBHOOK_URL)
# ---------------------------------------------------------------------------

async def main() -> None:
    init_db()
    load_all_reminders_into_scheduler()
    scheduler.start()

    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("HTTP-сервер запущен на порту %s", PORT)

    webhook_url = os.getenv("WEBHOOK_URL", "").strip()
    if webhook_url:
        full_url = f"{webhook_url}/webhook/{BOT_TOKEN}"
        await bot.set_webhook(full_url)
        log.info("Бот работает через webhook: %s", full_url)
        while True:
            await asyncio.sleep(3600)
    else:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("Бот работает в режиме polling")
        await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
