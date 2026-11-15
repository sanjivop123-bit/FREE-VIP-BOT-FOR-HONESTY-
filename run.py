"""HONESTY AI VIP BOT launcher.   Render start command:  python run.py

Loads main-4.py unchanged and adds:
  1) photo welcome message with 5 buttons            (texts: WELCOME / PAGES section)
  2) AUTOMATIC SIGNALS from Twelve Data               (chart image + entry / expiry / CALL-PUT / analysis)
     - sent to every APPROVED user (admins get a copy), users can /stopsignals and /startsignals
Render environment variables for signals:
  TWELVE_DATA_API_KEY   (required)      SIGNAL_PAIRS      default EUR/USD,GBP/USD,USD/JPY,AUD/USD,USD/CAD,EUR/GBP
  SIGNAL_EVERY_MIN 15   SIGNAL_MIN_SCORE 4 (of 5)        SIGNAL_EXPIRY_MIN 2     SIGNAL_INTERVAL 1min
  SIGNAL_COOLDOWN_MIN 30   SIGNAL_SCAN_DELAY 8 (seconds between API calls)
  SIGNAL_UTC_OFFSET_MIN 330   SIGNAL_TZ_LABEL IST
Admin commands: /autosignal  (status)  /autosignal on|off  /autosignal now  /autosignal test EURUSD
User commands : /stopsignals  /startsignals
The chart image is drawn in pure Python (no extra packages needed).
"""
import asyncio
import importlib.util
import os
import re
import struct
import sys
import time
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, LinkPreviewOptions
from telegram.error import RetryAfter
from telegram.ext import CallbackQueryHandler, CommandHandler, filters

HERE = Path(__file__).resolve().parent

spec = importlib.util.spec_from_file_location("bot_main", HERE / "main-4.py")
bot_main = importlib.util.module_from_spec(spec)
sys.modules["bot_main"] = bot_main  # needed by dataclasses inside main-4.py
spec.loader.exec_module(bot_main)
log = bot_main.logger

# =====================================================================
# 1) WELCOME TEXTS  (edit freely)
# =====================================================================
WELCOME_TEXT = """❤️‍🔥 Hello {first_name}

➡️ Welcome to FREE VIP BOT

🔥 Thanks for contacting us!

👇 SELECT YOUR OPTION BELOW 👇

📲 MESSAGE ADMIN: @honestytamil

🪩 PUBLIC GROUP:
https://t.me/honestyxz
"""

JOIN_TEXT = """❤️‍🔥 FREE VIP JOINING PROCESS

➡️ CREATE NEW ACCOUNT WITH THIS LINK

🔗 https://broker-qx.pro/sign-up/?lid=2416000

➡️ DEPOSIT AMOUNT MINIMUM 40$

➡️ AFTER DEPOSIT, SEND YOUR TRADER ID

🔥 START YOUR TRADING JOURNEY WITH US!

📈 TRADE RESPONSIBLY WITH PROPER MONEY MANAGEMENT.
"""

NEW_ACCOUNT_TEXT = """❤️‍🔥 HOW TO CREATE QUOTEX NEW ACCOUNT

➡️ 1. Click on my registration link:

🔗 https://broker-qx.pro/sign-up/?lid=2416000

➡️ 2. Select your Country.

➡️ 3. Enter your Email and create a strong Password.

➡️ 4. Accept Terms & Conditions.

➡️ 5. Click on Registration.

➡️ 6. Check your Email and verify your account using the verification link.

📈 DONE! YOUR ACCOUNT REGISTRATION PROCESS IS COMPLETE.

🔥 After registration, you can send your Trader ID to me
"""

GROUP_TEXT = """❤️‍🔥 WELCOME TO OUR OFFICIAL PUBLIC GROUP

🔥 Join Our Trading Community Today!

📢 GET DAILY UPDATES
📊 TRADING SIGNAL UPDATES
💬 COMMUNITY DISCUSSIONS
🚀 LATEST NEWS & ANNOUNCEMENTS

👇 CLICK BELOW TO JOIN OUR PUBLIC GROUP

🔗 https://t.me/honestyxz

❤️‍🔥 THANK YOU FOR BEING PART OF OUR COMMUNITY!

🔥 STAY CONNECTED & TRADE RESPONSIBLY!
"""

HELP_TEXT = """🔵 NEED HELP? WE ARE HERE FOR YOU! ❤️‍🔥

👋 Hello Trader!

❓ Do you have any questions or problems?

📲 CONTACT OUR SUPPORT:

👉 Telegram: @honestytamil

💬 Click the username below to message us directly:

🔗 https://t.me/honestytamil

❤️‍🔥 We are happy to help you!

🔥 THANK YOU FOR CONTACTING FREE VIP BOT.
"""

DELETE_TEXT = """🔴 HOW TO DELETE QUOTEX OLD ACCOUNT

➡️1. Go to Profile option.

➡️2. Scroll down and select Delete Account.

➡️3. Check your email - you will receive a link for deleting the account.

➡️4. Click on the link you received on email - your account is deleted successfully.

Now you can Register a new account using our link:
🔗https://broker-qx.pro/sign-up/?lid=2416000
"""

# button id -> (text, show the "Submit my Trader ID" button under it)
PAGES = {
    "join": (JOIN_TEXT, True),
    "new": (NEW_ACCOUNT_TEXT, True),
    "group": (GROUP_TEXT, False),
    "help": (HELP_TEXT, False),
    "old": (DELETE_TEXT, False),
}

APPROVED_NOTE = ("\n\n📈 Automatic signals will now arrive here.\n"
                 "/stopsignals - stop signals\n/startsignals - start again")

# =====================================================================
# 2) SIGNAL SETTINGS (from Render environment variables)
# =====================================================================


def _int(name, default):
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _pairs(raw):
    out = []
    for p in raw.split(","):
        p = p.strip().upper()
        if re.match(r"^[A-Z]{3}/[A-Z]{3}$", p) and p not in out:
            out.append(p)
    return out


TD_KEY = os.environ.get("TWELVE_DATA_API_KEY", "").strip()
PAIRS = _pairs(os.environ.get("SIGNAL_PAIRS", "EUR/USD,GBP/USD,USD/JPY,AUD/USD,USD/CAD,EUR/GBP")) or ["EUR/USD"]
INTERVAL = os.environ.get("SIGNAL_INTERVAL", "1min").strip()
if INTERVAL not in ("1min", "5min", "15min"):
    INTERVAL = "1min"
STALE_MIN = {"1min": 6, "5min": 20, "15min": 50}[INTERVAL]  # older last candle = market closed
EXPIRY = _int("SIGNAL_EXPIRY_MIN", 2)
EVERY_MIN = max(1, _int("SIGNAL_EVERY_MIN", 15))
MIN_SCORE = min(5, max(1, _int("SIGNAL_MIN_SCORE", 4)))
COOLDOWN_MIN = _int("SIGNAL_COOLDOWN_MIN", 30)
SCAN_DELAY = max(0, _int("SIGNAL_SCAN_DELAY", 8))  # Twelve Data free plan: 8 requests per minute
UTC_OFFSET_MIN = _int("SIGNAL_UTC_OFFSET_MIN", 330)
TZ_LABEL = os.environ.get("SIGNAL_TZ_LABEL", "IST").strip() or "IST"
ANALYSIS_LABEL = "Analysis"  # text before the analysis sentence in the signal

# ============================ PURE LOGIC (indicators, signal, chart) ============================


def _ema(values, n):
    if len(values) < n:
        return []
    k = 2 / (n + 1)
    out = [sum(values[:n]) / n]
    for v in values[n:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _rsi(values, n=14):
    if len(values) < n + 1:
        return None
    d = [b - a for a, b in zip(values, values[1:])]
    g = sum(max(x, 0) for x in d[:n]) / n
    lo = sum(max(-x, 0) for x in d[:n]) / n
    for x in d[n:]:
        g = (g * (n - 1) + max(x, 0)) / n
        lo = (lo * (n - 1) + max(-x, 0)) / n
    if g == 0 and lo == 0:
        return 50.0
    return 100.0 if lo == 0 else 100 - 100 / (1 + g / lo)


def _macd_hist(values):
    fast, slow = _ema(values, 12), _ema(values, 26)
    if not slow:
        return None
    line = [f - s for f, s in zip(fast[len(fast) - len(slow):], slow)]
    sig = _ema(line, 9)
    return (line[-1] - sig[-1]) if sig else None


def evaluate(candles):
    """candles: oldest -> newest list of {'o','h','l','c'}. Returns score -5..+5 (+ = CALL) and a short analysis."""
    closes = [c["c"] for c in candles]
    if len(closes) < 40:
        return None
    price = closes[-1]
    e9, e21 = _ema(closes, 9)[-1], _ema(closes, 21)[-1]
    r, h, hp = _rsi(closes), _macd_hist(closes), _macd_hist(closes[:-1])
    if r is None or h is None or hp is None or not price:
        return None
    atr = sum(c["h"] - c["l"] for c in candles[-14:]) / 14
    flat = atr / price < 2e-5
    score, pro = 0, {1: [], -1: []}

    def add(v, text):
        nonlocal score
        score += v
        pro[v].append(text)

    add(1 if e9 > e21 else -1, "EMA 9 is above EMA 21" if e9 > e21 else "EMA 9 is below EMA 21")
    add(1 if price > e21 else -1, "price is trading above the 21 EMA" if price > e21 else "price is trading below the 21 EMA")
    if h > 0 and h > hp:
        add(1, "MACD momentum is rising")
    elif h < 0 and h < hp:
        add(-1, "MACD momentum is falling")
    if r >= 80:
        add(-1, f"RSI at {r:.0f} is overbought")
    elif r > 50:
        add(1, f"RSI at {r:.0f} is in the bullish zone")
    elif r <= 20:
        add(1, f"RSI at {r:.0f} is oversold")
    else:
        add(-1, f"RSI at {r:.0f} is in the bearish zone")
    last = candles[-3:]
    if all(b["h"] > a["h"] and b["l"] > a["l"] for a, b in zip(last, last[1:])):
        add(1, "recent candles make higher highs and higher lows")
    elif all(b["h"] < a["h"] and b["l"] < a["l"] for a, b in zip(last, last[1:])):
        add(-1, "recent candles make lower highs and lower lows")
    side = 1 if score >= 0 else -1
    parts = pro[side][:4]
    head = "Short-term momentum looks bullish: " if side == 1 else "Short-term momentum looks bearish: "
    return {"score": score, "direction": "CALL" if side == 1 else "PUT", "flat": flat,
            "analysis": head + ", ".join(parts) + "." if parts else "Indicators are mixed."}


W, H = 900, 500
BG, GRID, UP, DN = (13, 17, 23), (30, 36, 44), (38, 166, 91), (239, 83, 80)


def render_chart(candles, direction):
    """Candlestick chart as PNG bytes (dark theme, last-price line, CALL/PUT arrow on the last candle)."""
    cs = candles[-60:]
    n = len(cs)
    lo, hi = min(c["l"] for c in cs), max(c["h"] for c in cs)
    pad = (hi - lo) * 0.1 or abs(hi) * 1e-4 or 1e-4
    lo, hi = lo - pad, hi + pad
    left, right, top, bottom = 12, W - 78, 14, H - 14
    pw, ph = right - left, bottom - top
    buf = bytearray(bytes(BG) * (W * H))

    def y(p):
        return max(top, min(bottom, int(bottom - (p - lo) / (hi - lo) * ph)))

    def rect(x0, y0, x1, y1, col):
        x0, x1 = max(0, min(W - 1, x0)), max(0, min(W - 1, x1))
        y0, y1 = max(0, min(H - 1, y0)), max(0, min(H - 1, y1))
        if x1 < x0 or y1 < y0:
            return
        row = bytes(col) * (x1 - x0 + 1)
        for yy in range(y0, y1 + 1):
            o = (yy * W + x0) * 3
            buf[o:o + len(row)] = row

    for k in range(1, 6):
        rect(left, top + ph * k // 6, right, top + ph * k // 6, GRID)
    for k in range(0, n, 10):
        gx = left + int(pw * (k + 0.5) / n)
        rect(gx, top, gx, bottom, GRID)
    slot = pw / n
    half = max(3, int(slot * 0.62)) // 2
    for i, c in enumerate(cs):
        cx = left + int(slot * (i + 0.5))
        col = UP if c["c"] >= c["o"] else DN
        rect(cx, y(c["h"]), cx, y(c["l"]), col)
        y1, y2 = sorted((y(c["o"]), y(c["c"])))
        rect(cx - half, y1, cx + half, max(y2, y1 + 1), col)
    mark = UP if direction == "CALL" else DN
    ly = y(cs[-1]["c"])
    for x in range(left, right, 12):
        rect(x, ly, x + 6, ly, mark)
    cx, size = left + int(slot * (n - 0.5)), 9
    if direction == "CALL":
        t0 = y(cs[-1]["l"]) + 6
        for k in range(size):
            rect(cx - k, t0 + k, cx + k, t0 + k, UP)
    else:
        t0 = y(cs[-1]["h"]) - 6 - size
        for k in range(size):
            rect(cx - (size - 1 - k), t0 + k, cx + (size - 1 - k), t0 + k, DN)
    raw = b"".join(b"\x00" + bytes(buf[r * W * 3:(r + 1) * W * 3]) for r in range(H))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


# ============================ END PURE LOGIC ============================

STOP_KB = InlineKeyboardMarkup([[InlineKeyboardButton("🔕 Stop signals", callback_data="sig:stop")]])
START_KB = InlineKeyboardMarkup([[InlineKeyboardButton("🔔 Start signals again", callback_data="sig:start")]])
LINE = "━━━━━━━━━━━━━━━━━━"


def build_caption(pair, res, test=False):
    now = datetime.now(timezone.utc)
    entry = (now + timedelta(seconds=60)).replace(second=0, microsecond=0) + timedelta(minutes=1)  # 1-2 min ahead
    local = entry.astimezone(timezone(timedelta(minutes=UTC_OFFSET_MIN))).strftime("%H:%M:%S")
    sig = "🟢 CALL" if res["direction"] == "CALL" else "🔴 PUT"
    lines = (["🧪 TEST SIGNAL (only you can see this)"] if test else []) + [
        "🔷 HONESTY AI VIP BOT", LINE,
        f"📊 Asset: {pair}", f"🕐 Entry Time: {local} {TZ_LABEL}", f"⏳ Expiry: {EXPIRY} Minutes",
        f"🎯 Signal: {sig}", f"💪 Strength: {abs(res['score'])}/5",
        f"🧠 {ANALYSIS_LABEL}: {res['analysis']}", LINE,
        "⚠️ Trade responsibly. No signal is guaranteed and you can lose your money. "
        "Prices can differ slightly from your broker."]
    return "\n".join(lines)


# ---- stop / start preference (PostgreSQL if DATABASE_URL is set, else memory) ----
async def _ensure_table(app):
    if app.bot_data.get("optout_ready"):
        return
    pool = getattr(app.bot_data["store"], "_pool", None)
    if pool is not None:
        await pool.execute("CREATE TABLE IF NOT EXISTS bot_signal_optout (telegram_id BIGINT PRIMARY KEY)")
    app.bot_data["optout_ready"] = True


async def optouts(app):
    await _ensure_table(app)
    pool = getattr(app.bot_data["store"], "_pool", None)
    if pool is not None:
        return {r["telegram_id"] for r in await pool.fetch("SELECT telegram_id FROM bot_signal_optout")}
    return app.bot_data.setdefault("optout", set())


async def set_optout(app, uid, off):
    await _ensure_table(app)
    pool = getattr(app.bot_data["store"], "_pool", None)
    if pool is not None:
        if off:
            await pool.execute("INSERT INTO bot_signal_optout VALUES ($1) ON CONFLICT DO NOTHING", uid)
        else:
            await pool.execute("DELETE FROM bot_signal_optout WHERE telegram_id=$1", uid)
    else:
        s = app.bot_data.setdefault("optout", set())
        s.add(uid) if off else s.discard(uid)


# ---- Twelve Data ----
async def td_candles(symbol):
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get("https://api.twelvedata.com/time_series", params={
            "symbol": symbol, "interval": INTERVAL, "outputsize": 80, "timezone": "UTC", "apikey": TD_KEY})
    data = resp.json()
    if data.get("status") == "error" or "values" not in data:
        raise RuntimeError(f"Twelve Data error code {data.get('code')}")
    out = []
    for v in reversed(data["values"]):  # API gives newest first
        out.append({"t": datetime.strptime(v["datetime"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc),
                    "o": float(v["open"]), "h": float(v["high"]), "l": float(v["low"]), "c": float(v["close"])})
    return out


async def scan(app, force=False):
    """Check every pair one by one. Returns (best_setup_or_None, notes_for_admin)."""
    best, notes, cool = None, [], app.bot_data.setdefault("cool", {})
    for i, pair in enumerate(PAIRS):
        if i:
            await asyncio.sleep(SCAN_DELAY)
        try:
            candles = await td_candles(pair)
        except RuntimeError as exc:
            notes.append(f"{pair}: {exc}")
            if "429" in str(exc):
                break  # rate limit: stop this round
            continue
        except Exception as exc:  # noqa: BLE001 - type only (error text may hold the API key)
            notes.append(f"{pair}: data error ({type(exc).__name__})")
            continue
        age = (datetime.now(timezone.utc) - candles[-1]["t"]).total_seconds() / 60 if candles else 9999
        if age > STALE_MIN:
            notes.append(f"{pair}: market closed (last candle {age / 60:.1f} h old)" if age > 120
                         else f"{pair}: no fresh candles ({age:.0f} min old)")
            continue
        res = evaluate(candles)
        if res is None:
            notes.append(f"{pair}: not enough data")
            continue
        tag = " (flat market)" if res["flat"] else ""
        notes.append(f"{pair}: score {res['score']:+d}{tag}")
        if res["flat"] or abs(res["score"]) < MIN_SCORE:
            continue
        if not force and time.time() - cool.get(pair, 0) < COOLDOWN_MIN * 60:
            notes[-1] += " (cooldown)"
            continue
        if best is None or abs(res["score"]) > abs(best["score"]):
            best = {**res, "pair": pair, "candles": candles}
    return best, notes


async def send_signal(app, caption, png, only=None):
    """Send chart + caption. The photo is uploaded once, then Telegram's file_id is reused for everybody."""
    if only is None:
        approved = set(await app.bot_data["store"].list_approved_ids())
        ids = (approved | set(app.bot_data["settings"].admin_ids)) - await optouts(app)
    else:
        ids = set(only)
    file_id, sent = None, 0
    for uid in sorted(ids):
        for _ in range(2):
            try:
                m = await app.bot.send_photo(uid, file_id or InputFile(png, filename="signal.png"),
                                             caption=caption, reply_markup=STOP_KB)
                file_id = file_id or m.photo[-1].file_id
                sent += 1
                break
            except RetryAfter as exc:
                ra = exc.retry_after
                await asyncio.sleep((ra.total_seconds() if hasattr(ra, "total_seconds") else ra) + 1)
            except Exception as exc:  # noqa: BLE001 - blocked bot, deleted user, ...
                log.warning("signal not delivered to %s: %s", uid, type(exc).__name__)
                break
        await asyncio.sleep(0.05)  # stay under Telegram's ~30 messages/second
    return sent, len(ids)


async def run_cycle(app, force=False):
    best, notes = await scan(app, force)
    if best:
        png = await asyncio.to_thread(render_chart, best["candles"], best["direction"])
        sent, total = await send_signal(app, build_caption(best["pair"], best), png)
        app.bot_data.setdefault("cool", {})[best["pair"]] = time.time()
        notes.append(f"SENT {best['pair']} {best['direction']} to {sent}/{total} users")
    else:
        notes.append("No setup strong enough this round (nothing sent).")
    return notes


async def signal_engine(app):
    while app.bot_data.get("store") is None or not app.running:  # wait until the bot is fully started
        await asyncio.sleep(2)
    app.bot_data.setdefault("auto_on", True)
    if not TD_KEY:
        log.warning("TWELVE_DATA_API_KEY is not set: automatic signals are disabled")
        return
    log.info("automatic signals ON: %d pairs, every %d min, min strength %d/5", len(PAIRS), EVERY_MIN, MIN_SCORE)
    await asyncio.sleep(20)
    while True:
        try:
            if app.bot_data.get("auto_on"):
                notes = await run_cycle(app)
                log.info("signal round: %s", " | ".join(notes))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("signal round failed: %s", type(exc).__name__)
        await asyncio.sleep(EVERY_MIN * 60)


def _spawn(app, coro):
    async def safe():
        try:
            await coro
        except Exception as exc:  # noqa: BLE001
            log.error("background task failed: %s", type(exc).__name__)

    task = asyncio.get_running_loop().create_task(safe())
    tasks = app.bot_data.setdefault("tasks", set())
    tasks.add(task)
    task.add_done_callback(tasks.discard)


# ---- Telegram handlers ----
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
WELCOME_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("🔵 FREE VIP JOINING PROCESS", callback_data="w:join")],
    [InlineKeyboardButton("🟢 HOW TO CREATE QUOTEX NEW ACCOUNT", callback_data="w:new")],
    [InlineKeyboardButton("🔵 I WANT TO JOIN PUBLIC GROUP", callback_data="w:group")],
    [InlineKeyboardButton("🔵 ASK ANY QUESTION OR PROBLEM", callback_data="w:help")],
    [InlineKeyboardButton("🔴 HOW TO DELETE QUOTEX OLD ACCOUNT", callback_data="w:old")],
])
SUBMIT_KB = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Submit my Trader ID", callback_data="submit")]])
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}


def _find_photo():
    files = sorted(p for p in HERE.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)
    return files[0] if files else None


async def welcome(update, context):
    user = update.effective_user
    text = WELCOME_TEXT.replace("{first_name}", user.first_name if user and user.first_name else "Trader")
    msg = update.effective_message
    photo = _find_photo()
    if photo:
        data = context.application.bot_data
        try:
            sent = await msg.reply_photo(photo=data.get("welcome_file_id") or photo.read_bytes(),
                                         caption=text, reply_markup=WELCOME_KB)
            data["welcome_file_id"] = sent.photo[-1].file_id  # reuse Telegram's copy next time
            return
        except Exception as exc:  # noqa: BLE001
            data.pop("welcome_file_id", None)
            log.warning("welcome photo failed: %s", type(exc).__name__)
    await msg.reply_text(text, reply_markup=WELCOME_KB, link_preview_options=NO_PREVIEW)


async def on_welcome_button(update, context):
    q = update.callback_query
    await q.answer()
    text, with_submit = PAGES[q.data.split(":", 1)[1]]
    await q.message.reply_text(text, reply_markup=SUBMIT_KB if with_submit else None,
                               link_preview_options=NO_PREVIEW)


async def _is_member(app, uid):
    row = await app.bot_data["store"].get_by_user(uid)
    return bool(row and row["status"] == bot_main.APPROVED) or uid in app.bot_data["settings"].admin_ids


async def _set_pref(app, uid, stop):
    """Returns the text to show the user."""
    if stop:
        await set_optout(app, uid, True)
        return "🔕 Signals stopped. You will not get automatic signals any more."
    if not await _is_member(app, uid):
        return "Only approved members get signals. Send your Trader ID with /verify first."
    await set_optout(app, uid, False)
    return "🔔 Signals are ON again. New signals will arrive here automatically."


async def stop_cmd(update, context):
    await update.effective_message.reply_text(
        await _set_pref(context.application, update.effective_user.id, True), reply_markup=START_KB)


async def start_signals_cmd(update, context):
    await update.effective_message.reply_text(
        await _set_pref(context.application, update.effective_user.id, False))


async def on_sig_button(update, context):
    q = update.callback_query
    await q.answer()
    stop = q.data == "sig:stop"
    text = await _set_pref(context.application, q.from_user.id, stop)
    await q.message.reply_text(text, reply_markup=START_KB if stop else None)


def _pair_arg(raw):
    p = raw.strip().upper().replace("-", "/")
    if "/" not in p and len(p) == 6:
        p = p[:3] + "/" + p[3:]
    return p if re.match(r"^[A-Z]{3}/[A-Z]{3}$", p) else None


async def autosignal_cmd(update, context):
    if not bot_main._is_admin(update, context):
        return
    app, msg = context.application, update.effective_message
    arg = (context.args[0].lower() if context.args else "")
    if arg in ("on", "off"):
        app.bot_data["auto_on"] = arg == "on"
        await msg.reply_text(f"Automatic signals: {arg.upper()}")
        return
    if arg in ("now", "test"):
        if not TD_KEY:
            await msg.reply_text("Add TWELVE_DATA_API_KEY in Render -> Environment first.")
            return
        chat_id, uid = msg.chat_id, update.effective_user.id
        if arg == "now":
            await msg.reply_text(f"Scanning {len(PAIRS)} pairs... result in about {len(PAIRS) * SCAN_DELAY // 60 + 1} min.")

            async def job():
                notes = await run_cycle(app, force=True)
                await app.bot.send_message(chat_id, "Scan result:\n" + "\n".join(notes))
        else:
            pair = _pair_arg(context.args[1]) if len(context.args) > 1 else PAIRS[0]
            if pair is None:
                await msg.reply_text("Usage: /autosignal test EURUSD")
                return
            await msg.reply_text(f"Building a test signal for {pair}...")

            async def job():
                candles = await td_candles(pair)
                res = evaluate(candles)
                if res is None:
                    await app.bot.send_message(chat_id, "Not enough data for a test signal.")
                    return
                png = await asyncio.to_thread(render_chart, candles, res["direction"])
                await send_signal(app, build_caption(pair, res, test=True), png, only=[uid])

        _spawn(app, job())
        return
    approved = set(await app.bot_data["store"].list_approved_ids())
    stopped = len(approved & await optouts(app))
    await msg.reply_text(
        f"Automatic signals: {'ON' if app.bot_data.get('auto_on', True) else 'OFF'}\n"
        f"Twelve Data key: {'set' if TD_KEY else 'MISSING (add TWELVE_DATA_API_KEY)'}\n"
        f"Pairs: {', '.join(PAIRS)}\nEvery {EVERY_MIN} min | candles {INTERVAL} | expiry {EXPIRY} min | "
        f"min strength {MIN_SCORE}/5\nApproved users: {len(approved)} (stopped: {stopped})\n\n"
        "/autosignal on|off   /autosignal now   /autosignal test EURUSD")


# ---- hook everything into main-4.py ----
_orig_build = bot_main.build_application
_orig_make_http = bot_main.make_http_app
_orig_approval = bot_main.approval_text


def _build(settings, polling):
    app = _orig_build(settings, polling)
    private = filters.ChatType.PRIVATE
    app.add_handler(CallbackQueryHandler(on_welcome_button, pattern=r"^w:(join|new|group|help|old)$"))
    app.add_handler(CallbackQueryHandler(on_sig_button, pattern=r"^sig:(stop|start)$"))
    app.add_handler(CommandHandler("stopsignals", stop_cmd, filters=private))
    app.add_handler(CommandHandler("startsignals", start_signals_cmd, filters=private))
    app.add_handler(CommandHandler("autosignal", autosignal_cmd))
    return app


def _make_http(ctx):
    asyncio.get_running_loop().create_task(signal_engine(ctx.ptb))  # runs inside the bot's event loop
    return _orig_make_http(ctx)


bot_main.start = welcome  # /start and /help use the new welcome
bot_main.build_application = _build
bot_main.make_http_app = _make_http
bot_main.approval_text = lambda s, row: _orig_approval(s, row) + APPROVED_NOTE

if __name__ == "__main__":
    bot_main.main()
