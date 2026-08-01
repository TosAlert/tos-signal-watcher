import os
import re
import sqlite3
import logging
from datetime import datetime, timezone

import yfinance as yf
from telegram import Bot, Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("watcher")

# ---------------------------------------------------------------------------
# Konfiguratsiya (Railway'da Variables bo'limiga qo'shiladi)
# ---------------------------------------------------------------------------
# READ_BOT_TOKEN — Signal kanalini FAQAT o'qish uchun. Bot1 (tos-telegram-bot)
# bilan bir xil tokenni ishlatishingiz mumkin, chunki u allaqachon Signal
# kanalida admin. Bu bot hech qachon shu token bilan xabar yubormaydi.
READ_BOT_TOKEN = os.getenv("READ_BOT_TOKEN")
SIGNAL_CHANNEL_ID = int(os.getenv("SIGNAL_CHANNEL_ID", "0"))

# RESULTS_BOT_TOKEN — BotFather'dan olingan YANGI, alohida token. Bu bot
# Natijalar kanaliga admin qilib qo'shilishi kerak. Faqat shu token orqali,
# faqat shu kanalga yoziladi.
RESULTS_BOT_TOKEN = os.getenv("RESULTS_BOT_TOKEN")
RESULTS_CHANNEL_ID = int(os.getenv("RESULTS_CHANNEL_ID", "0"))

TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "5"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "3"))
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))

DB_PATH = os.getenv("DB_PATH", "positions.db")

# Signal xabarlaridagi "Ticker: XXXX" qatorini o'qiydi (bot1 xabar formatiga mos)
TICKER_RE = re.compile(r"Ticker:\s*([A-Za-z.]{1,10})", re.IGNORECASE)

results_bot = Bot(token=RESULTS_BOT_TOKEN) if RESULTS_BOT_TOKEN else None


# ---------------------------------------------------------------------------
# Ma'lumotlar bazasi
# ---------------------------------------------------------------------------
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            entry_price REAL NOT NULL,
            entry_time TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            close_price REAL,
            close_time TEXT,
            result TEXT,
            pct REAL
        )
    """)
    conn.commit()
    return conn


def has_open_position(conn, ticker):
    cur = conn.execute(
        "SELECT id FROM positions WHERE ticker = ? AND status = 'open'", (ticker,)
    )
    return cur.fetchone() is not None


def add_position(conn, ticker, entry_price):
    conn.execute(
        "INSERT INTO positions (ticker, entry_price, entry_time, status) VALUES (?, ?, ?, 'open')",
        (ticker, entry_price, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def get_open_positions(conn):
    cur = conn.execute(
        "SELECT id, ticker, entry_price, entry_time FROM positions WHERE status = 'open'"
    )
    return cur.fetchall()


def close_position(conn, pos_id, close_price, result, pct):
    conn.execute(
        """UPDATE positions
           SET status = 'closed', close_price = ?, close_time = ?, result = ?, pct = ?
           WHERE id = ?""",
        (close_price, datetime.now(timezone.utc).isoformat(), result, pct, pos_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Narx olish (yfinance)
# ---------------------------------------------------------------------------
def get_current_price(ticker):
    try:
        t = yf.Ticker(ticker)
        fi = getattr(t, "fast_info", None)
        price = fi.get("lastPrice") if fi else None
        if price:
            return float(price)
    except Exception as e:
        log.warning(f"[Price] fast_info xato ({ticker}): {e}")

    try:
        hist = yf.Ticker(ticker).history(period="1d")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception as e:
        log.warning(f"[Price] history xato ({ticker}): {e}")

    return None


# ---------------------------------------------------------------------------
# 1) Signal kanalini FAQAT o'qiydi — hech qachon bu yerga yozmaydi
# ---------------------------------------------------------------------------
async def handle_signal_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None or msg.text is None:
        return

    if update.effective_chat is None or update.effective_chat.id != SIGNAL_CHANNEL_ID:
        return  # boshqa chat/kanallardan kelgan narsalarni e'tiborsiz qoldiramiz

    match = TICKER_RE.search(msg.text)
    if not match:
        return

    ticker = match.group(1).upper()

    conn = db_connect()
    try:
        if has_open_position(conn, ticker):
            log.info(f"[Signal] {ticker} allaqachon kuzatuvda, o'tkazib yuboramiz")
            return

        entry_price = get_current_price(ticker)
        if entry_price is None:
            log.warning(f"[Signal] {ticker} narxi olinmadi, kuzatuvga qo'shilmadi")
            return

        add_position(conn, ticker, entry_price)
        log.info(f"[Signal] Yangi kuzatuv qo'shildi: {ticker} @ {entry_price:.2f}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2) Davriy tekshiruv — TP/SL yetganda FAQAT Natijalar kanaliga yozadi
# ---------------------------------------------------------------------------
async def check_positions_job(context: ContextTypes.DEFAULT_TYPE):
    conn = db_connect()
    try:
        open_positions = get_open_positions(conn)
        if not open_positions:
            return

        log.info(f"[Check] {len(open_positions)} ta ochiq pozitsiya tekshirilmoqda")

        for pos_id, ticker, entry_price, entry_time in open_positions:
            current_price = get_current_price(ticker)
            if current_price is None:
                continue

            pct = (current_price - entry_price) / entry_price * 100

            if pct >= TAKE_PROFIT_PCT:
                close_position(conn, pos_id, current_price, "TAKE_PROFIT", pct)
                await send_result(ticker, entry_price, current_price, pct, "✅ FOYDADA YOPILDI (Take-Profit)")
            elif pct <= -STOP_LOSS_PCT:
                close_position(conn, pos_id, current_price, "STOP_LOSS", pct)
                await send_result(ticker, entry_price, current_price, pct, "🔴 ZARARDA YOPILDI (Stop-Loss)")
    finally:
        conn.close()


async def send_result(ticker, entry_price, close_price, pct, title):
    text = (
        f"{title}\n\n"
        f"📌 Ticker: {ticker}\n"
        f"🎯 Kirish narxi: ${entry_price:.2f}\n"
        f"🏁 Chiqish narxi: ${close_price:.2f}\n"
        f"📊 Natija: {pct:+.2f}%"
    )
    if results_bot is None:
        log.error("[Result] RESULTS_BOT_TOKEN sozlanmagan, yuborilmadi")
        return
    try:
        await results_bot.send_message(chat_id=RESULTS_CHANNEL_ID, text=text)
        log.info(f"[Result] Yuborildi: {ticker} ({pct:+.2f}%)")
    except Exception as e:
        log.error(f"[Result] Yuborishda xato ({ticker}): {e}")


# ---------------------------------------------------------------------------
# Ishga tushirish
# ---------------------------------------------------------------------------
def main():
    missing = [
        name for name, val in [
            ("READ_BOT_TOKEN", READ_BOT_TOKEN),
            ("RESULTS_BOT_TOKEN", RESULTS_BOT_TOKEN),
            ("SIGNAL_CHANNEL_ID", SIGNAL_CHANNEL_ID or None),
            ("RESULTS_CHANNEL_ID", RESULTS_CHANNEL_ID or None),
        ] if not val
    ]
    if missing:
        raise RuntimeError(f"Quyidagi environment variable(lar) yo'q: {', '.join(missing)}")

    db_connect().close()  # jadval mavjudligiga ishonch hosil qilamiz

    app = Application.builder().token(READ_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, handle_signal_message))
    app.job_queue.run_repeating(
        check_positions_job,
        interval=CHECK_INTERVAL_MINUTES * 60,
        first=15,
    )

    log.info("Watcher bot ishga tushdi ✅")
    log.info(f"Signal kanali: {SIGNAL_CHANNEL_ID} (faqat o'qiladi)")
    log.info(f"Natijalar kanali: {RESULTS_CHANNEL_ID} (faqat shu yerga yoziladi)")
    log.info(
        f"TP: +{TAKE_PROFIT_PCT}% | SL: -{STOP_LOSS_PCT}% | "
        f"Tekshiruv: har {CHECK_INTERVAL_MINUTES} daqiqada"
    )

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
