from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timezone

from aiohttp import web
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------- تنظیمات (از متغیرهای محیطی Railway خوانده می‌شود) ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "raffle.db")

if not BOT_TOKEN or ADMIN_ID == 0:
    raise SystemExit("خطا: BOT_TOKEN و ADMIN_ID در تنظیمات Railway تعریف نشده‌اند.")

CARD_NUMBER = "6037997547142712"
CARD_HOLDER = "کیوان صحرائی"
AMOUNT = "500 هزار تومان"

WELCOME_TEXT = (
    "👋 سلام! به ربات قرعه‌کشی خوش آمدید.\n\n"
    "برای شرکت در قرعه‌کشی لطفاً دکمه «🎟 شروع» را بزنید."
)

START_TEXT = (
    "باسلام\n\n"
    "برای شرکت در قرعه‌کشی و دریافت کد قرعه، لطفاً مبلغ 500هزارتومان را به شماره کارت زیر واریز کنید:\n\n"
    f"💳 `{CARD_NUMBER}`\n"
    f"به نام {CARD_HOLDER}\n\n"
    "⚠️فیش واریز (عکس) را ارسال نمایید و منتظر کد قرعه بمانید.\n\n"
    "🎟 پس از تایید فیش کد قرعه سه‌رقمی و غیرتکراری از صادر می‌شود."
)

USER_HELP_TEXT = (
    "📖 راهنمای شرکت در قرعه‌کشی\n\n"
    "1️⃣ روی دکمه «🎟 شروع» بزنید.\n"
    "2️⃣ مبلغ را به شماره کارت واریز کنید.\n"
    "3️⃣ روی دکمه «📸 ارسال رسید» بزنید و عکس فیش را بفرستید.\n"
    "4️⃣ نام و نام خانوادگی خود را (به فارسی) ارسال کنید.\n"
    "5️⃣ منتظر تایید مدیر بمانید تا کد قرعه شما صادر شود.\n\n"
    "⚠️ هر کاربر در هر دوره فقط یک‌بار می‌تواند شرکت کند."
)

ADMIN_HELP_TEXT = (
    "📖 راهنمای مدیر:\n\n"
    "• 📋 لیست شرکت‌کنندگان\n"
    "• 📊 آمار دوره\n"
    "• 🔄 شروع دوره جدید\n"
    "• 🚫 بن / ✅ آن‌بن (با ارسال آیدی عددی)\n"
    "• 📖 راهنما\n\n"
    "برای تایید یا رد فیش‌ها از دکمه‌های زیر هر فیش استفاده کنید."
)

# ---------- دیتابیس ----------
DB_LOCK = threading.RLock()
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA journal_mode=WAL;")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db() -> None:
    with DB_LOCK:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT OR IGNORE INTO meta(key, value) VALUES ('round', '1');
            CREATE TABLE IF NOT EXISTS participants (
                id INTEGER PRIMARY KEY AUTOINCREMENT, round INTEGER NOT NULL,
                user_id INTEGER NOT NULL, username TEXT, tg_first_name TEXT, full_name TEXT,
                receipt_file_id TEXT NOT NULL, receipt_type TEXT NOT NULL DEFAULT 'photo',
                receipt_message_id INTEGER, status TEXT NOT NULL DEFAULT 'pending',
                code INTEGER, created_at TEXT NOT NULL, approved_at TEXT, completed_at TEXT,
                UNIQUE (round, user_id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_participants_round_code ON participants(round, code) WHERE code IS NOT NULL;
            CREATE TABLE IF NOT EXISTS banned_users (user_id INTEGER PRIMARY KEY, banned_at TEXT NOT NULL);
            """
        )
        conn.commit()


def get_current_round() -> int:
    with DB_LOCK:
        row = conn.execute("SELECT value FROM meta WHERE key='round'").fetchone()
        return int(row["value"])


def start_new_round() -> int:
    with DB_LOCK:
        new_round = get_current_round() + 1
        conn.execute("UPDATE meta SET value=? WHERE key='round'", (str(new_round),))
        conn.commit()
        return new_round


def get_participant(round_no: int, user_id: int):
    with DB_LOCK:
        return conn.execute("SELECT * FROM participants WHERE round=? AND user_id=?", (round_no, user_id)).fetchone()


def upsert_participant(round_no, user_id, username, tg_first_name, file_id, message_id, full_name) -> None:
    with DB_LOCK:
        conn.execute(
            """
            INSERT INTO participants (round, user_id, username, tg_first_name, receipt_file_id, receipt_type, receipt_message_id, full_name, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'photo', ?, ?, 'pending', ?)
            ON CONFLICT(round, user_id) DO UPDATE SET
                username=excluded.username, tg_first_name=excluded.tg_first_name, receipt_file_id=excluded.receipt_file_id,
                receipt_message_id=excluded.receipt_message_id, full_name=excluded.full_name, status='pending',
                code=NULL, created_at=excluded.created_at, approved_at=NULL, completed_at=NULL
            """,
            (round_no, user_id, username, tg_first_name, file_id, message_id, full_name, now_iso()),
        )
        conn.commit()


def approve_participant(round_no: int, user_id: int) -> bool:
    with DB_LOCK:
        cur = conn.execute("UPDATE participants SET status='approved', approved_at=? WHERE round=? AND user_id=? AND status='pending'", (now_iso(), round_no, user_id))
        conn.commit()
        return cur.rowcount == 1


def reject_participant(round_no: int, user_id: int) -> bool:
    with DB_LOCK:
        cur = conn.execute("UPDATE participants SET status='rejected', approved_at=? WHERE round=? AND user_id=? AND status='pending'", (now_iso(), round_no, user_id))
        conn.commit()
        return cur.rowcount == 1


def is_banned(user_id: int) -> bool:
    with DB_LOCK:
        return conn.execute("SELECT 1 FROM banned_users WHERE user_id=?", (user_id,)).fetchone() is not None


def ban_user(user_id: int) -> None:
    with DB_LOCK:
        conn.execute("INSERT OR IGNORE INTO banned_users (user_id, banned_at) VALUES (?, ?)", (user_id, now_iso()))
        conn.commit()


def unban_user(user_id: int) -> None:
    with DB_LOCK:
        conn.execute("DELETE FROM banned_users WHERE user_id=?", (user_id,))
        conn.commit()


def assign_code(round_no, user_id):
    with DB_LOCK:
        row = conn.execute("SELECT status FROM participants WHERE round=? AND user_id=?", (round_no, user_id)).fetchone()
        if not row or row["status"] != "approved":
            return None
        used = {r["code"] for r in conn.execute("SELECT code FROM participants WHERE round=? AND code IS NOT NULL", (round_no,)).fetchall()}
        available = [c for c in range(100, 600) if c not in used]
        if not available:
            return None
        code = secrets.choice(available)
        cur = conn.execute("UPDATE participants SET code=?, status='completed', completed_at=? WHERE round=? AND user_id=? AND status='approved'", (code, now_iso(), round_no, user_id))
        conn.commit()
        return code if cur.rowcount == 1 else None


def get_all_participants(round_no: int):
    with DB_LOCK:
        return conn.execute("SELECT * FROM participants WHERE round=? ORDER BY CASE status WHEN 'completed' THEN 0 WHEN 'approved' THEN 1 WHEN 'pending' THEN 2 ELSE 3 END, code IS NULL, code, id", (round_no,)).fetchall()

STATUS_FA = {"pending": "در انتظار", "approved": "منتظر کد", "rejected": "رد شده", "completed": "تکمیل شده"}

def format_participants_list(round_no, rows) -> str:
    if not rows: return f"📋 لیست دوره {round_no} خالی است."
    completed = sum(1 for r in rows if r["status"] == "completed")
    approved = sum(1 for r in rows if r["status"] == "approved")
    pending = sum(1 for r in rows if r["status"] == "pending")
    lines = [f"📋 لیست دوره {round_no}", f"تکمیل: {completed} | منتظر کد: {approved} | در انتظار: {pending} | مجموع: {len(rows)}", "—" * 22]
    for i, r in enumerate(rows, 1):
        name = r["full_name"] or "—"
        code = r["code"] if r["code"] is not None else "—"
        uname = f"@{r['username']}" if r["username"] else f"ID:{r['user_id']}"
        lines.append(f"{i}. {name} | کد: {code} | {STATUS_FA.get(r['status'], r['status'])} | {uname}")
    return "\n".join(lines)

def split_text(text: str, limit: int = 4000) -> list[str]:
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current += line
    if current: chunks.append(current)
    return chunks

async def send_participants_list(bot, chat_id, round_no):
    for chunk in split_text(format_participants_list(round_no, get_all_participants(round_no))):
        await bot.send_message(chat_id=chat_id, text=chunk)

# ---------- کیبوردها ----------
def admin_menu_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton("📋 لیست شرکت‌کنندگان"), KeyboardButton("📊 آمار دوره")], [KeyboardButton("🔄 شروع دوره جدید"), KeyboardButton("📖 راهنما")], [KeyboardButton("🚫 بن"), KeyboardButton("✅ آن‌بن")]], resize_keyboard=True)

def user_menu_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton("🎟 شروع"), KeyboardButton("📖 راهنما")]], resize_keyboard=True)

def receipt_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton("📸 ارسال رسید")]], resize_keyboard=True, one_time_keyboard=True)

def reset_confirm_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton("✅ بله، ریست کن"), KeyboardButton("❌ لغو")]], resize_keyboard=True, one_time_keyboard=True)

# ---------- توابع ربات ----------
async def forward_receipt_to_admin(context, user, participant, round_no):
    caption = f"🧾 فیش جدید\nدوره: {round_no}\nنام: {participant['full_name']}\nیوزرنیم: @{user.username if user.username else '—'}\nآیدی: {user.id}"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ تایید", callback_data=f"approve:{user.id}:{round_no}"), InlineKeyboardButton("❌ رد", callback_data=f"reject:{user.id}:{round_no}")]])
    await context.bot.send_photo(chat_id=ADMIN_ID, photo=participant["receipt_file_id"], caption=caption, reply_markup=keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_banned(user.id): return await update.message.reply_text("⛔ شما مسدود شده‌اید.")
    if user.id == ADMIN_ID: return await update.message.reply_text("👨‍💼 پنل مدیر", reply_markup=admin_menu_keyboard())
    await update.message.reply_text(WELCOME_TEXT, reply_markup=user_menu_keyboard())

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user = update.message, update.effective_user
    if is_banned(user.id): return await msg.reply_text("⛔ شما مسدود شده‌اید.")
    if user.id == ADMIN_ID: return
    if not context.user_data.get("awaiting_receipt"): return await msg.reply_text("ابتدا «🎟 شروع» را بزنید.", reply_markup=user_menu_keyboard())
    
    round_no = get_current_round()
    if get_participant(round_no, user.id): return await msg.reply_text("شما قبلاً ثبت‌نام کرده‌اید.", reply_markup=user_menu_keyboard())

    context.user_data.update({"temp_receipt_file_id": msg.photo[-1].file_id, "temp_receipt_msg_id": msg.message_id, "awaiting_receipt": False, "awaiting_name": True})
    await msg.reply_text("✅ عکس دریافت شد.\nنام و نام خانوادگی خود را (فقط فارسی) وارد کنید:", reply_markup=ReplyKeyboardMarkup([["❌ لغو"]], resize_keyboard=True))

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user = update.message, update.effective_user
    if not msg.text: return
    if is_banned(user.id): return await msg.reply_text("⛔ شما مسدود شده‌اید.")
    text = msg.text.strip()
    
    if user.id == ADMIN_ID:
        if context.user_data.get("admin_action"):
            if text == "❌ لغو":
                context.user_data.pop("admin_action")
                return await msg.reply_text("لغو شد.", reply_markup=admin_menu_keyboard())
            if text.isdigit():
                action = context.user_data.pop("admin_action")
                ban_user(int(text)) if action == "ban" else unban_user(int(text))
                return await msg.reply_text(f"✅ انجام شد.", reply_markup=admin_menu_keyboard())
            return await msg.reply_text("❌ آیدی معتبر بفرستید یا «❌ لغو» بزنید.")
            
        if text == "🚫 بن": context.user_data["admin_action"] = "ban"; return await msg.reply_text("آیدی عددی جهت بن:", reply_markup=ReplyKeyboardMarkup([["❌ لغو"]], resize_keyboard=True))
        if text == "✅ آن‌بن": context.user_data["admin_action"] = "unban"; return await msg.reply_text("آیدی عددی جهت آن‌بن:", reply_markup=ReplyKeyboardMarkup([["❌ لغو"]], resize_keyboard=True))
        
        if context.user_data.get("awaiting_reset_confirm"):
            if text == "✅ بله، ریست کن":
                context.user_data.pop("awaiting_reset_confirm")
                new_round = start_new_round()
                return await msg.reply_text(f"✅ دوره {new_round} آغاز شد.", reply_markup=admin_menu_keyboard())
            if text == "❌ لغو":
                context.user_data.pop("awaiting_reset_confirm")
                return await msg.reply_text("ریست لغو شد.", reply_markup=admin_menu_keyboard())

        if text == "📋 لیست شرکت‌کنندگان": return await send_participants_list(context.bot, msg.chat_id, get_current_round())
        if text == "📊 آمار دوره":
            rows = get_all_participants(get_current_round())
            return await msg.reply_text(f"📊 آمار دوره {get_current_round()}\nتکمیل: {sum(1 for r in rows if r['status']=='completed')}\nمنتظر کد: {sum(1 for r in rows if r['status']=='approved')}\nدر انتظار: {sum(1 for r in rows if r['status']=='pending')}\nمجموع: {len(rows)}", reply_markup=admin_menu_keyboard())
        if text == "🔄 شروع دوره جدید": context.user_data["awaiting_reset_confirm"] = True; return await msg.reply_text("مطمئنید؟", reply_markup=reset_confirm_keyboard())
        if text == "📖 راهنما": return await msg.reply_text(ADMIN_HELP_TEXT, reply_markup=admin_menu_keyboard())
        return await msg.reply_text("از دکمه‌ها استفاده کنید.", reply_markup=admin_menu_keyboard())

    if text == "❌ لغو":
        context.user_data.clear()
        return await msg.reply_text("عملیات لغو شد.", reply_markup=user_menu_keyboard())
    
    if context.user_data.get("awaiting_name"):
        if not re.match(r'^[\u0600-\u06FF\s]+$', text) or len(text) < 5:
            return await msg.reply_text("❌ فقط حروف فارسی و نام کامل وارد کنید.")
        round_no = get_current_round()
        upsert_participant(round_no, user.id, user.username, user.first_name or "", context.user_data.get("temp_receipt_file_id"), context.user_data.get("temp_receipt_msg_id"), text)
        context.user_data.clear()
        await forward_receipt_to_admin(context, user, get_participant(round_no, user.id), round_no)
        return await msg.reply_text("✅ اطلاعات ارسال شد. منتظر تایید بمانید.", reply_markup=user_menu_keyboard())

    if text == "📖 راهنما": return await msg.reply_text(USER_HELP_TEXT, reply_markup=user_menu_keyboard())
    if text == "🎟 شروع":
        participant = get_participant(get_current_round(), user.id)
        if participant:
            status = participant["status"]
            if status == "completed": return await msg.reply_text(f"🎟 کد شما: `{participant['code']}`", parse_mode="Markdown")
            if status == "pending": return await msg.reply_text("⏳ در انتظار تایید مدیر.")
            if status == "approved": return await msg.reply_text("✅ تایید شده، در حال پردازش...")
            if status == "rejected": return await msg.reply_text("❌ رسید رد شد.")
            return
        return await msg.reply_text(START_TEXT, reply_markup=receipt_keyboard(), parse_mode="Markdown")
    
    if text == "📸 ارسال رسید":
        if get_participant(get_current_round(), user.id): return await msg.reply_text("شما قبلاً ثبت‌نام کرده‌اید.")
        context.user_data["awaiting_receipt"] = True
        return await msg.reply_text("عکس رسید را بفرستید:")
        
    await msg.reply_text("راهنما: «📖 راهنما»", reply_markup=user_menu_keyboard())

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID: return await q.answer("⛔", show_alert=True)
    action, uid, rnd = q.data.split(":")
    uid, rnd = int(uid), int(rnd)
    
    if action == "approve":
        if not approve_participant(rnd, uid): return await q.answer("قبلاً بررسی شده.", show_alert=True)
        code = assign_code(rnd, uid)
        await q.answer("✅ تایید شد.")
        await q.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(uid, f"✅ رسید تایید شد.\n🎟 کد قرعه شما: `{code}`", parse_mode="Markdown", reply_markup=user_menu_keyboard())
        await q.message.reply_text(f"✅ تایید شد.\n👤 آیدی: `{uid}`\n🎟 کد: `{code}`", parse_mode="Markdown")
    elif action == "reject":
        if not reject_participant(rnd, uid): return await q.answer("قبلاً بررسی شده.", show_alert=True)
        await q.answer("❌ رد شد.")
        await q.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(uid, "❌ رسید شما تایید نشد.", reply_markup=user_menu_keyboard())

# ---------- وب سرور ساختگی برای Railway ----------
async def health_check(request):
    return web.Response(text="Bot is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Web server started on port {port}")

# ---------- اجرای اصلی ----------
async def main():
    init_db()
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    application.add_handler(CallbackQueryHandler(admin_callback))
    
    # شروع وب سرور در پس‌زمینه تا Railway ربات را نخواباند
    asyncio.create_task(start_web_server())
    
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
    stop_signal = asyncio.Event()
    await stop_signal.wait()

if __name__ == "__main__":
    asyncio.run(main())