import os
import re
import csv
import io as _io
import sqlite3
import logging
from datetime import datetime, timezone, timedelta, time as dt_time

import requests
from telegram import Bot, Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("watcher")

# ---------------------------------------------------------------------------
# Konfiguratsiya (Railway'da Variables bo'limiga qo'shiladi)
# ---------------------------------------------------------------------------
READ_BOT_TOKEN = os.getenv("READ_BOT_TOKEN")
SIGNAL_CHANNEL_ID = int(os.getenv("SIGNAL_CHANNEL_ID", "0"))

RESULTS_BOT_TOKEN = os.getenv("RESULTS_BOT_TOKEN")
RESULTS_CHANNEL_ID = int(os.getenv("RESULTS_CHANNEL_ID", "0"))

TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "10"))   # trailing rejim shu foizdan keyin yoqiladi
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "5"))
TRAIL_PCT = float(os.getenv("TRAIL_PCT", "3"))                # eng yuqori narxdan necha % pasayganda yopilsin
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))

# Hisobotlar Toshkent vaqti bilan soat 09:00da yuboriladi (UTC+5 -> 04:00 UTC)
REPORT_HOUR_UTC = int(os.getenv("REPORT_HOUR_UTC", "4"))
TASHKENT_OFFSET_HOURS = 5

# MUHIM: Railway'da bu yo'l albatta Volume'ga ko'rsatilishi kerak
# (masalan /data/positions.db), aks holda har deployda ma'lumotlar o'chadi.
DB_PATH = os.getenv("DB_PATH", "positions.db")

TICKER_RE = re.compile(r"Ticker:\s*([A-Za-z.]{1,10})", re.IGNORECASE)
ALGO_RE = re.compile(r"Algorithm:\s*([^\n\r]+)", re.IGNORECASE)

results_bot = Bot(token=RESULTS_BOT_TOKEN) if RESULTS_BOT_TOKEN else None

try:
    from curl_cffi import requests as cffi_requests

    _yf_session = cffi_requests.Session(impersonate="chrome")
except Exception as e:
    log.warning(f"[Price] curl_cffi mavjud emas, oddiy session ishlatiladi: {e}")
    _yf_session = None


def _tashkent_now():
    return datetime.now(timezone.utc) + timedelta(hours=TASHKENT_OFFSET_HOURS)


# ---------------------------------------------------------------------------
# Ma'lumotlar bazasi
# ---------------------------------------------------------------------------
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            algorithm TEXT,
            entry_price REAL NOT NULL,
            entry_time TEXT NOT NULL,
            peak_price REAL NOT NULL,
            trail_active INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'open',
            close_price REAL,
            close_time TEXT,
            result TEXT,
            pct REAL
        )
    """)
    conn.commit()

    # Migratsiya: Volume'da eski sxema bilan yaratilgan positions.db bo'lishi
    # mumkin (algorithm/peak_price/trail_active ustunlarisiz). Yetishmayotgan
    # ustunlarni xavfsiz qo'shib qo'yamiz.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(positions)")}
    migrations = {
        "algorithm": "ALTER TABLE positions ADD COLUMN algorithm TEXT",
        "peak_price": "ALTER TABLE positions ADD COLUMN peak_price REAL NOT NULL DEFAULT 0",
        "trail_active": "ALTER TABLE positions ADD COLUMN trail_active INTEGER NOT NULL DEFAULT 0",
    }
    for col, sql in migrations.items():
        if col not in existing_cols:
            log.info(f"[DB] Migratsiya: '{col}' ustuni qo'shilmoqda")
            conn.execute(sql)
    conn.commit()

    # peak_price hali 0 bo'lgan eski qatorlarni entry_price bilan to'ldiramiz
    conn.execute(
        "UPDATE positions SET peak_price = entry_price WHERE peak_price = 0 OR peak_price IS NULL"
    )
    conn.commit()

    return conn


def has_open_position(conn, ticker):
    cur = conn.execute(
        "SELECT id FROM positions WHERE ticker = ? AND status = 'open'", (ticker,)
    )
    return cur.fetchone() is not None


def add_position(conn, ticker, algorithm, entry_price):
    conn.execute(
        """INSERT INTO positions
           (ticker, algorithm, entry_price, entry_time, peak_price, trail_active, status)
           VALUES (?, ?, ?, ?, ?, 0, 'open')""",
        (ticker, algorithm, entry_price, datetime.now(timezone.utc).isoformat(), entry_price),
    )
    conn.commit()


def get_open_positions(conn):
    cur = conn.execute(
        """SELECT id, ticker, algorithm, entry_price, peak_price, trail_active
           FROM positions WHERE status = 'open'"""
    )
    return cur.fetchall()


def update_peak(conn, pos_id, peak_price):
    conn.execute("UPDATE positions SET peak_price = ? WHERE id = ?", (peak_price, pos_id))
    conn.commit()


def activate_trail(conn, pos_id, peak_price):
    conn.execute(
        "UPDATE positions SET trail_active = 1, peak_price = ? WHERE id = ?",
        (peak_price, pos_id),
    )
    conn.commit()


def close_position(conn, pos_id, close_price, result, pct):
    conn.execute(
        """UPDATE positions
           SET status = 'closed', close_price = ?, close_time = ?, result = ?, pct = ?
           WHERE id = ?""",
        (close_price, datetime.now(timezone.utc).isoformat(), result, pct, pos_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Narx olish (Yahoo Finance chart API to'g'ridan-to'g'ri, keyin Stooq zaxira)
# ---------------------------------------------------------------------------
_YAHOO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


def _get_price_yahoo(ticker):
    """
    yfinance kutubxonasi o'rniga Yahoo'ning ochiq chart API'siga to'g'ridan-to'g'ri
    murojaat qilamiz — bu yfinance + curl_cffi orasidagi nomuvofiqlikdan qochadi.
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {"interval": "1d", "range": "5d"}
    try:
        if _yf_session:
            resp = _yf_session.get(url, params=params, timeout=10)
        else:
            resp = requests.get(url, params=params, timeout=10, headers=_YAHOO_HEADERS)

        if resp.status_code != 200:
            log.warning(f"[Price] Yahoo HTTP {resp.status_code} ({ticker})")
            return None

        data = resp.json()
        result = (data.get("chart") or {}).get("result")
        if not result:
            return None

        meta = result[0].get("meta", {}) or {}
        price = meta.get("regularMarketPrice")
        if price:
            return float(price)

        closes = result[0]["indicators"]["quote"][0]["close"]
        closes = [c for c in closes if c is not None]
        if closes:
            return float(closes[-1])
    except Exception as e:
        log.warning(f"[Price] Yahoo xato ({ticker}): {e}")

    return None


def _get_price_stooq(ticker):
    """
    Zaxira narx manbai — Stooq.com'ning bepul tarixiy CSV endpointi.
    To'g'ri yo'l /q/d/l/ (kunlik tarixiy ma'lumot), /q/l/ EMAS.
    """
    url = f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d"
    try:
        resp = requests.get(url, timeout=10, headers=_YAHOO_HEADERS)
        resp.raise_for_status()

        text = resp.text.strip()
        if not text or "No data" in text or "Exceeded" in text:
            return None

        rows = list(csv.DictReader(_io.StringIO(text)))
        if not rows:
            return None

        close = rows[-1].get("Close")
        if close in (None, "", "N/D"):
            return None
        return float(close)
    except Exception as e:
        log.warning(f"[Price] Stooq xato ({ticker}): {e}")
        return None


def get_current_price(ticker):
    price = _get_price_yahoo(ticker)
    if price is not None:
        return price

    log.info(f"[Price] {ticker}: Yahoo muvaffaqiyatsiz, Stooq'ga o'tamiz")
    price = _get_price_stooq(ticker)
    if price is not None:
        return price

    log.warning(f"[Price] {ticker}: hech qaysi manbadan narx olinmadi")
    return None


# ---------------------------------------------------------------------------
# 1) Signal kanalini FAQAT o'qiydi — hech qachon bu yerga yozmaydi
# ---------------------------------------------------------------------------
async def handle_signal_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None:
        return

    text = msg.text or msg.caption
    if text is None:
        return

    if update.effective_chat is None or update.effective_chat.id != SIGNAL_CHANNEL_ID:
        return

    match = TICKER_RE.search(text)
    if not match:
        return

    ticker = match.group(1).upper()

    algo_match = ALGO_RE.search(text)
    algorithm = algo_match.group(1).strip() if algo_match else "Noma'lum"

    conn = db_connect()
    try:
        if has_open_position(conn, ticker):
            log.info(f"[Signal] {ticker} allaqachon kuzatuvda, o'tkazib yuboramiz")
            return

        entry_price = get_current_price(ticker)
        if entry_price is None:
            log.warning(f"[Signal] {ticker} narxi olinmadi, kuzatuvga qo'shilmadi")
            return

        add_position(conn, ticker, algorithm, entry_price)
        log.info(f"[Signal] Yangi kuzatuv qo'shildi: {ticker} ({algorithm}) @ {entry_price:.2f}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2) Davriy tekshiruv — Trailing Take-Profit / Stop-Loss
# ---------------------------------------------------------------------------
async def check_positions_job(context: ContextTypes.DEFAULT_TYPE):
    conn = db_connect()
    try:
        open_positions = get_open_positions(conn)
        if not open_positions:
            return

        log.info(f"[Check] {len(open_positions)} ta ochiq pozitsiya tekshirilmoqda")

        for pos_id, ticker, algorithm, entry_price, peak_price, trail_active in open_positions:
            current_price = get_current_price(ticker)
            if current_price is None:
                continue

            pct_from_entry = (current_price - entry_price) / entry_price * 100

            if current_price > peak_price:
                peak_price = current_price
                update_peak(conn, pos_id, peak_price)

            if not trail_active:
                if pct_from_entry <= -STOP_LOSS_PCT:
                    close_position(conn, pos_id, current_price, "STOP_LOSS", pct_from_entry)
                    await send_close_result(
                        ticker, algorithm, entry_price, current_price, pct_from_entry,
                        "🔴 ZARARDA YOPILDI (Stop-Loss)",
                    )
                    continue

                if pct_from_entry >= TAKE_PROFIT_PCT:
                    activate_trail(conn, pos_id, peak_price)
                    log.info(
                        f"[Trail] {ticker}: +{pct_from_entry:.2f}% ga yetdi, "
                        f"trailing take-profit yoqildi (peak=${peak_price:.2f})"
                    )
                    continue
            else:
                drawdown_from_peak = (peak_price - current_price) / peak_price * 100
                if drawdown_from_peak >= TRAIL_PCT:
                    close_position(conn, pos_id, current_price, "TAKE_PROFIT", pct_from_entry)
                    await send_close_result(
                        ticker, algorithm, entry_price, current_price, pct_from_entry,
                        "✅ FOYDADA YOPILDI (Trailing Take-Profit)",
                    )
                    continue
    finally:
        conn.close()


async def send_text(text):
    if results_bot is None:
        log.error("[Result] RESULTS_BOT_TOKEN sozlanmagan, yuborilmadi")
        return
    try:
        await results_bot.send_message(chat_id=RESULTS_CHANNEL_ID, text=text)
    except Exception as e:
        log.error(f"[Result] Yuborishda xato: {e}")


async def send_close_result(ticker, algorithm, entry_price, close_price, pct, title):
    text = (
        f"{title}\n\n"
        f"📌 Ticker: {ticker}\n"
        f"🧠 Signal: {algorithm}\n"
        f"🎯 Kirish narxi: ${entry_price:.2f}\n"
        f"🏁 Chiqish narxi: ${close_price:.2f}\n"
        f"📊 Natija: {pct:+.2f}%"
    )
    await send_text(text)
    log.info(f"[Result] Yuborildi: {ticker} ({pct:+.2f}%)")


# ---------------------------------------------------------------------------
# 3) Kunlik hisobot — ochiq pozitsiyalar (har kuni soat 09:00, Toshkent)
# ---------------------------------------------------------------------------
async def daily_report_job(context: ContextTypes.DEFAULT_TYPE):
    conn = db_connect()
    try:
        rows = conn.execute(
            "SELECT ticker, algorithm, entry_price FROM positions WHERE status = 'open'"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        await send_text("📋 Kunlik hisobot\n\nHozircha ochiq pozitsiya yo'q.")
        return

    lines = ["📋 Kunlik hisobot — ochiq pozitsiyalar\n"]
    for ticker, algorithm, entry_price in rows:
        current = get_current_price(ticker)
        if current:
            pct = (current - entry_price) / entry_price * 100
            lines.append(f"• {ticker} ({algorithm}) — kirish: ${entry_price:.2f}, hozir: {pct:+.2f}%")
        else:
            lines.append(f"• {ticker} ({algorithm}) — kirish: ${entry_price:.2f}, hozirgi narx olinmadi")

    await send_text("\n".join(lines))


# ---------------------------------------------------------------------------
# 4) Haftalik hisobot — algoritm bo'yicha (har Dushanba, soat 09:00)
# ---------------------------------------------------------------------------
def _algo_stats(rows):
    stats = {}
    for algorithm, status, result in rows:
        algorithm = algorithm or "Noma'lum"
        s = stats.setdefault(algorithm, {"total": 0, "profit": 0, "loss": 0})
        s["total"] += 1
        if status == "closed":
            if result == "TAKE_PROFIT":
                s["profit"] += 1
            elif result == "STOP_LOSS":
                s["loss"] += 1
    return stats


async def weekly_report_job(context: ContextTypes.DEFAULT_TYPE):
    now = _tashkent_now()
    if now.weekday() != 0:
        return

    since = (now - timedelta(days=7)).astimezone(timezone.utc).isoformat()
    conn = db_connect()
    try:
        rows = conn.execute(
            "SELECT algorithm, status, result FROM positions WHERE entry_time >= ?",
            (since,),
        ).fetchall()
    finally:
        conn.close()

    stats = _algo_stats(rows)
    lines = ["📊 Haftalik hisobot (oxirgi 7 kun)\n"]
    if not stats:
        lines.append("Bu hafta signal kelmadi.")
    else:
        for algorithm, s in sorted(stats.items(), key=lambda x: -x[1]["total"]):
            lines.append(
                f"• {algorithm}: {s['total']} ta signal — ✅ {s['profit']} foydada, 🔴 {s['loss']} zararda"
            )

    await send_text("\n".join(lines))


# ---------------------------------------------------------------------------
# 5) Oylik hisobot — win-rate va eng kuchli signal (har oyning 1-kuni, 09:00)
# ---------------------------------------------------------------------------
async def monthly_report_job(context: ContextTypes.DEFAULT_TYPE):
    now = _tashkent_now()
    if now.day != 1:
        return

    since = (now - timedelta(days=30)).astimezone(timezone.utc).isoformat()
    conn = db_connect()
    try:
        rows = conn.execute(
            "SELECT algorithm, status, result FROM positions WHERE entry_time >= ?",
            (since,),
        ).fetchall()
    finally:
        conn.close()

    stats = _algo_stats(rows)
    lines = ["📈 Oylik hisobot (oxirgi 30 kun)\n"]

    if not stats:
        lines.append("Bu oy signal kelmadi.")
        await send_text("\n".join(lines))
        return

    best_algo = None
    best_wr = -1.0
    for algorithm, s in sorted(stats.items(), key=lambda x: -x[1]["total"]):
        closed = s["profit"] + s["loss"]
        wr = (s["profit"] / closed * 100) if closed else 0.0
        lines.append(
            f"• {algorithm}: {s['total']} ta signal, win-rate: {wr:.0f}% ({s['profit']}/{closed})"
        )
        if closed >= 3 and wr > best_wr:
            best_wr = wr
            best_algo = algorithm

    if best_algo:
        lines.append(f"\n🏆 Eng kuchli signal: {best_algo} ({best_wr:.0f}% win-rate)")
    else:
        lines.append("\nEng kuchli signalni aniqlash uchun yetarli yopilgan pozitsiya yo'q (kamida 3 ta kerak).")

    await send_text("\n".join(lines))


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

    db_connect().close()

    app = Application.builder().token(READ_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, handle_signal_message))

    app.job_queue.run_repeating(
        check_positions_job,
        interval=CHECK_INTERVAL_MINUTES * 60,
        first=15,
    )

    report_time = dt_time(hour=REPORT_HOUR_UTC, minute=0, second=0, tzinfo=timezone.utc)
    app.job_queue.run_daily(daily_report_job, time=report_time)
    app.job_queue.run_daily(weekly_report_job, time=report_time)
    app.job_queue.run_daily(monthly_report_job, time=report_time)

    log.info("Watcher bot ishga tushdi ✅")
    log.info(f"Signal kanali: {SIGNAL_CHANNEL_ID} (faqat o'qiladi)")
    log.info(f"Natijalar kanali: {RESULTS_CHANNEL_ID} (faqat shu yerga yoziladi)")
    log.info(
        f"TP: +{TAKE_PROFIT_PCT}% (trailing {TRAIL_PCT}%) | SL: -{STOP_LOSS_PCT}% | "
        f"Tekshiruv: har {CHECK_INTERVAL_MINUTES} daqiqada"
    )
    log.info(f"Hisobotlar: har kuni soat {REPORT_HOUR_UTC:02d}:00 UTC (Toshkent 09:00)")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
