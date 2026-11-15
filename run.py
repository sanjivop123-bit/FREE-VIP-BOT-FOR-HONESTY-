"""HONESTY AI VIP BOT launcher  -  UPGRADED: continuous 1m + 5m multi-pair signal engine.
Render start command:  python run.py        (main-4.py must sit next to this file, unchanged)

Loads main-4.py unchanged and adds:
  1) photo welcome message with 5 buttons                 (unchanged from the previous run.py)
  2) CONTINUOUS AUTOMATIC SIGNAL ENGINE (Twelve Data)     1-minute and 5-minute candles, ALL pairs
     - scans every pair on every CLOSED 1m / 5m candle, sends every valid confirmed signal
     - chart image = real candlesticks of the pair/timeframe (price scale, times, last price, CALL/PUT arrow)
     - duplicate prevention (one signal per pair+timeframe+candle, cooldown, safe across restarts with PostgreSQL)
     - API rate limiting (requests/minute + daily budget, 429 handling, no wasted calls on stale jobs)
     - START ENGINE / STOP ENGINE buttons for admins (/engine), state survives restarts with PostgreSQL

Render environment variables
  required : TWELVE_DATA_API_KEY
  pairs    : SIGNAL_PAIRS (default EUR/USD,GBP/USD,USD/JPY,AUD/USD,USD/CAD,EUR/GBP)
  timeframes: SIGNAL_TIMEFRAMES (default 1min,5min)
  strength : SIGNAL_MIN_SCORE 4 (of 5)   SIGNAL_MIN_SCORE_5M (default = SIGNAL_MIN_SCORE)
  expiry   : SIGNAL_EXPIRY_MIN 2 (1m signals)   SIGNAL_EXPIRY_5M_MIN 5 (5m signals)
  cooldown : SIGNAL_COOLDOWN_1M_MIN 5   SIGNAL_COOLDOWN_5M_MIN 15     (per pair + timeframe)
  API      : TD_RPM 8 (requests/minute allowed by your Twelve Data plan)   TD_DAILY_LIMIT 800 (0 = unlimited)
  timing   : SIGNAL_FETCH_OFFSET_SEC 4   SIGNAL_MAX_DELAY_SEC 45   SIGNAL_LEAD_SEC 30
  options  : SIGNAL_AUTOSTART 1   SIGNAL_REQUIRE_5M_ALIGN 0   SIGNAL_MAX_PER_ROUND 0 (0 = no cap)   KEEPALIVE 0
  display  : SIGNAL_UTC_OFFSET_MIN 330   SIGNAL_TZ_LABEL IST
  (no longer used: SIGNAL_EVERY_MIN, SIGNAL_INTERVAL, SIGNAL_SCAN_DELAY, SIGNAL_COOLDOWN_MIN - harmless if still set)
Admin commands: /engine (status + START/STOP buttons)  /startengine  /stopengine
                /autosignal  /autosignal on|off|now|test EURUSD [1m|5m]
User commands : /stopsignals  /startsignals
The chart image is drawn in pure Python (no extra packages needed).
"""
import asyncio
import importlib.util
import itertools
import os
import re
import struct
import sys
import time
import zlib
from collections import deque
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


def _flag(name, default):
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


TD_KEY = os.environ.get("TWELVE_DATA_API_KEY", "").strip()
PAIRS = _pairs(os.environ.get("SIGNAL_PAIRS", "EUR/USD,GBP/USD,USD/JPY,AUD/USD,USD/CAD,EUR/GBP")) or ["EUR/USD"]

TF_SECONDS = {"1min": 60, "5min": 300}
TF_LABEL = {"1min": "1M", "5min": "5M"}
_TF_ALIAS = {"1m": "1min", "1min": "1min", "1": "1min", "5m": "5min", "5min": "5min", "5": "5min"}


def _timeframes(raw):
    out = []
    for p in raw.split(","):
        tf = _TF_ALIAS.get(p.strip().lower())
        if tf and tf not in out:
            out.append(tf)
    return out


TIMEFRAMES = _timeframes(os.environ.get("SIGNAL_TIMEFRAMES", "1min,5min")) or ["1min", "5min"]
MIN_SCORE = min(5, max(1, _int("SIGNAL_MIN_SCORE", 4)))
MIN_SCORE_TF = {"1min": MIN_SCORE, "5min": min(5, max(1, _int("SIGNAL_MIN_SCORE_5M", MIN_SCORE)))}
EXPIRY = {"1min": max(1, _int("SIGNAL_EXPIRY_MIN", 2)), "5min": max(1, _int("SIGNAL_EXPIRY_5M_MIN", 5))}
COOLDOWN_MIN = {"1min": max(0, _int("SIGNAL_COOLDOWN_1M_MIN", 5)), "5min": max(0, _int("SIGNAL_COOLDOWN_5M_MIN", 15))}
STALE_MIN = {"1min": 6, "5min": 20}        # older last candle = market closed / no data
TD_RPM = max(1, _int("TD_RPM", 8))          # Twelve Data free plan: 8 requests per minute
TD_DAILY_LIMIT = max(0, _int("TD_DAILY_LIMIT", 800))   # free plan: ~800 per day; 0 = no daily limit
FETCH_OFFSET = max(0, _int("SIGNAL_FETCH_OFFSET_SEC", 4))   # wait after a candle closes before asking for data
MAX_DELAY = max(10, _int("SIGNAL_MAX_DELAY_SEC", 45))       # a signal older than this (1m; x2 for 5m) is dropped
LEAD_SEC = max(0, _int("SIGNAL_LEAD_SEC", 30))              # minimum seconds between sending and entry time
MAX_PER_ROUND = max(0, _int("SIGNAL_MAX_PER_ROUND", 0))     # 0 = send every valid signal
REQUIRE_ALIGN = _flag("SIGNAL_REQUIRE_5M_ALIGN", False)     # 1m signals only when the 5m trend agrees
AUTOSTART = _flag("SIGNAL_AUTOSTART", True)                 # engine state at first boot (later: saved state)
KEEPALIVE = _flag("KEEPALIVE", False)                       # ping own /health every 10 min (free Render plan)
UTC_OFFSET_MIN = _int("SIGNAL_UTC_OFFSET_MIN", 330)
TZ_LABEL = os.environ.get("SIGNAL_TZ_LABEL", "IST").strip() or "IST"
ANALYSIS_LABEL = "Analysis"  # text before the analysis sentence in the signal
RETRY_WAIT = 4               # seconds before the one retry when the provider is a few seconds late


def _now():
    """Epoch seconds. One place, so tests can control the clock."""
    return time.time()


def max_delay(tf):
    return MAX_DELAY * (2 if TF_SECONDS[tf] >= 300 else 1)


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



# ---- tiny 5x7 bitmap font (rows are 5-bit values, 7 rows per glyph) for chart labels ----
_FONT_SRC = {
    "A": "0E11111F111111", "B": "1E11111E11111E", "C": "0E11101010110E", "D": "1E11111111111E",
    "E": "1F10101E10101F", "F": "1F10101E101010", "G": "0E11101711110F", "H": "1111111F111111",
    "I": "0E04040404040E", "J": "0702020202120C", "K": "11121418141211", "L": "1010101010101F",
    "M": "111B1515111111", "N": "11111915131111", "O": "0E11111111110E", "P": "1E11111E101010",
    "Q": "0E11111115120D", "R": "1E11111E141211", "S": "0F10100E01011E", "T": "1F040404040404",
    "U": "1111111111110E", "V": "11111111110A04", "W": "1111111515150A", "X": "11110A040A1111",
    "Y": "11110A04040404", "Z": "1F01020408101F",
    "0": "0E11131519110E", "1": "040C040404040E", "2": "0E11010204081F", "3": "1F02040201110E",
    "4": "02060A121F0202", "5": "1F101E0101110E", "6": "0608101E11110E", "7": "1F010204080808",
    "8": "0E11110E11110E", "9": "0E11110F01020C",
    ".": "00000000000C0C", "/": "01010204081010", " ": "00000000000000", ":": "000C0C000C0C00",
    "-": "0000001F000000", "+": "0004041F040400",
}
GLYPHS = {ch: [int(h[i:i + 2], 16) for i in range(0, 14, 2)] for ch, h in _FONT_SRC.items()}

W, H = 900, 500
BG, GRID, UP, DN = (13, 17, 23), (30, 36, 44), (38, 166, 91), (239, 83, 80)
HDR, TXT, DIM = (20, 27, 36), (225, 230, 236), (125, 135, 148)


def render_chart(candles, direction, title="", tz_offset_min=0):
    """Candlestick chart as PNG bytes: title, price scale, time labels, last-price tag, CALL/PUT arrow."""
    cs = candles[-60:]
    n = len(cs)
    lo, hi = min(c["l"] for c in cs), max(c["h"] for c in cs)
    pad = (hi - lo) * 0.1 or abs(hi) * 1e-4 or 1e-4
    lo, hi = lo - pad, hi + pad
    left, right, top, bottom = 12, W - 100, 46, H - 30
    pw, ph = right - left, bottom - top
    buf = bytearray(bytes(BG) * (W * H))
    decimals = 3 if hi >= 20 else 5

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

    def text(x, y0, s, col, scale=2):
        cx = x
        for ch in str(s).upper():
            g = GLYPHS.get(ch)
            if g:
                for r, row in enumerate(g):
                    for c in range(5):
                        if row & (1 << (4 - c)):
                            rect(cx + c * scale, y0 + r * scale, cx + c * scale + scale - 1, y0 + r * scale + scale - 1, col)
            cx += 6 * scale
        return cx

    mark = UP if direction == "CALL" else DN
    rect(0, 0, W - 1, 38, HDR)
    text(14, 9, title, TXT, 3)
    tag = "CALL" if direction == "CALL" else "PUT"
    text(W - 14 - len(tag) * 18, 9, tag, mark, 3)

    for k in range(1, 6):
        gy = top + ph * k // 6
        rect(left, gy, right, gy, GRID)
        text(right + 8, gy - 5, f"{hi - (hi - lo) * k / 6:.{decimals}f}", DIM, 2)
    for k in range(n - 1, -1, -10):          # vertical grid + time label, counted from the newest candle
        gx = left + int(pw * (k + 0.5) / n)
        rect(gx, top, gx, bottom, GRID)
        t = cs[k].get("t")
        if t is not None and gx - 30 >= 0 and gx + 30 <= right + 60:
            text(gx - 30, bottom + 8, (t + timedelta(minutes=tz_offset_min)).strftime("%H:%M"), DIM, 2)
    slot = pw / n
    half = max(3, int(slot * 0.62)) // 2
    for i, c in enumerate(cs):
        cx = left + int(slot * (i + 0.5))
        col = UP if c["c"] >= c["o"] else DN
        rect(cx, y(c["h"]), cx, y(c["l"]), col)
        y1, y2 = sorted((y(c["o"]), y(c["c"])))
        rect(cx - half, y1, cx + half, max(y2, y1 + 1), col)
    ly = y(cs[-1]["c"])
    for x in range(left, right, 12):
        rect(x, ly, x + 6, ly, mark)
    rect(right + 2, ly - 9, W - 3, ly + 9, mark)
    text(right + 8, ly - 5, f"{cs[-1]['c']:.{decimals}f}", (255, 255, 255), 2)
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

    def chunk(tag_, data):
        return struct.pack(">I", len(data)) + tag_ + data + struct.pack(">I", zlib.crc32(tag_ + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


# ============================ END PURE LOGIC ============================

STOP_KB = InlineKeyboardMarkup([[InlineKeyboardButton("🔕 Stop signals", callback_data="sig:stop")]])
START_KB = InlineKeyboardMarkup([[InlineKeyboardButton("🔔 Start signals again", callback_data="sig:start")]])
LINE = "━━━━━━━━━━━━━━━━━━"


def _fmt_expiry(minutes):
    return f"{minutes} Minute" + ("" if minutes == 1 else "s")


def entry_time(now_ts):
    """Entry = the next whole minute that is at least SIGNAL_LEAD_SEC away."""
    return -(-int(now_ts + LEAD_SEC) // 60) * 60


def build_caption(pair, tf, res, entry_ts, aligned=False, test=False):
    local = datetime.fromtimestamp(entry_ts, timezone.utc).astimezone(
        timezone(timedelta(minutes=UTC_OFFSET_MIN))).strftime("%H:%M:%S")
    sig = "🟢 CALL" if res["direction"] == "CALL" else "🔴 PUT"
    lines = (["🧪 TEST SIGNAL (only you can see this)"] if test else []) + [
        "🔷 HONESTY AI VIP BOT", LINE,
        f"📊 Asset: {pair}", f"⏱ Timeframe: {TF_LABEL[tf]}", f"🕐 Entry Time: {local} {TZ_LABEL}",
        f"⏳ Expiry: {_fmt_expiry(EXPIRY[tf])}", f"🎯 Signal: {sig}", f"💪 Strength: {abs(res['score'])}/5"]
    if aligned:
        lines.append("🔗 Confirmation: 1M + 5M trend aligned")
    lines += [f"🧠 {ANALYSIS_LABEL}: {res['analysis']}", LINE,
              "⚠️ Trade responsibly. No signal is guaranteed and you can lose your money. "
              "Prices can differ slightly from your broker."]
    return "\n".join(lines)


# ---- database helpers (PostgreSQL if DATABASE_URL is set, else memory) ----
def _pool(app):
    return getattr(app.bot_data["store"], "_pool", None)


async def _ensure_table(app):
    if app.bot_data.get("tables_ready"):
        return
    pool = _pool(app)
    if pool is not None:
        await pool.execute("CREATE TABLE IF NOT EXISTS bot_signal_optout (telegram_id BIGINT PRIMARY KEY)")
        await pool.execute("CREATE TABLE IF NOT EXISTS bot_signal_log (sig_key TEXT PRIMARY KEY, pair TEXT NOT NULL, "
                           "tf TEXT NOT NULL, direction TEXT NOT NULL, score INT NOT NULL, "
                           "created_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        await pool.execute("CREATE TABLE IF NOT EXISTS bot_engine_state (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    app.bot_data["tables_ready"] = True


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


async def claim_signal(app, key, pair, tf, direction, score):
    """Atomically claim 'pair|timeframe|candle'. False = this signal was already sent (duplicate)."""
    claimed = app.bot_data.setdefault("claimed", {})
    if key in claimed:
        return False
    claimed[key] = _now()
    if len(claimed) > 5000:
        for k in sorted(claimed, key=claimed.get)[:2500]:
            claimed.pop(k, None)
    pool = _pool(app)
    if pool is not None:
        try:
            await _ensure_table(app)
            row = await pool.fetchrow(
                "INSERT INTO bot_signal_log (sig_key, pair, tf, direction, score) VALUES ($1,$2,$3,$4,$5) "
                "ON CONFLICT (sig_key) DO NOTHING RETURNING sig_key", key, pair, tf, direction, score)
            return row is not None      # None = another instance / earlier run already sent it
        except Exception as exc:  # noqa: BLE001
            log.warning("signal log unavailable (%s): using memory duplicate check", type(exc).__name__)
    return True


async def load_cooldowns(app):
    """After a restart: remember when each pair+timeframe last fired (PostgreSQL only)."""
    pool = _pool(app)
    cool = app.bot_data.setdefault("cool", {})
    if pool is None:
        return
    try:
        await _ensure_table(app)
        for r in await pool.fetch("SELECT pair, tf, max(created_at) AS ts FROM bot_signal_log GROUP BY pair, tf"):
            cool[(r["pair"], r["tf"])] = r["ts"].timestamp()
    except Exception as exc:  # noqa: BLE001
        log.warning("could not load cooldowns: %s", type(exc).__name__)


async def load_engine_state(app):
    """True / False = saved by an admin, None = never saved."""
    pool = _pool(app)
    if pool is None:
        return None
    try:
        await _ensure_table(app)
        row = await pool.fetchrow("SELECT v FROM bot_engine_state WHERE k='engine_on'")
        return None if row is None else row["v"] == "1"
    except Exception as exc:  # noqa: BLE001
        log.warning("could not load engine state: %s", type(exc).__name__)
        return None


async def save_engine_state(app, on):
    pool = _pool(app)
    if pool is None:
        return
    try:
        await _ensure_table(app)
        await pool.execute("INSERT INTO bot_engine_state (k, v) VALUES ('engine_on', $1) "
                           "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v", "1" if on else "0")
    except Exception as exc:  # noqa: BLE001
        log.warning("could not save engine state: %s", type(exc).__name__)


# ---- API rate limiting ----
class DailyLimit(Exception):
    """Daily API budget used up."""


class RateLimited(Exception):
    """Twelve Data answered 429."""


class TooLate(Exception):
    """Waiting for a free API slot would make the signal stale; no request was spent."""


class RateLimiter:
    """Sliding window: never more than `per_minute` requests in any `window` seconds, plus a daily budget."""

    def __init__(self, per_minute, daily=0, window=61.0):
        self.rpm, self.daily, self.window = max(1, per_minute), max(0, daily), window
        self.stamps = deque()
        self.lock = asyncio.Lock()
        self.day, self.count, self.day_blocked = None, 0, False
        self.blocked_until = 0.0

    def _roll(self):
        today = datetime.now(timezone.utc).date()
        if today != self.day:
            self.day, self.count, self.day_blocked = today, 0, False

    def used_today(self):
        self._roll()
        return self.count

    def used_recent(self):
        mono = time.monotonic()
        while self.stamps and mono - self.stamps[0] >= self.window:
            self.stamps.popleft()
        return len(self.stamps)

    def day_exhausted(self):
        self._roll()
        return self.day_blocked or bool(self.daily and self.count >= self.daily)

    def penalize(self, daily=False):
        """Called after a 429 from the provider."""
        self._roll()
        if daily:
            self.day_blocked = True
        else:
            self.blocked_until = max(self.blocked_until, time.monotonic() + max(5.0, 61.0 - (_now() % 60)))

    async def acquire(self, give_up_at=None):
        async with self.lock:
            while True:
                if self.day_exhausted():
                    raise DailyLimit()
                mono = time.monotonic()
                if mono < self.blocked_until:
                    wait = self.blocked_until - mono
                else:
                    if self.used_recent() < self.rpm:
                        self.stamps.append(mono)
                        self.count += 1
                        return
                    wait = self.stamps[0] + self.window - mono + 0.05
                if give_up_at is not None and _now() + wait > give_up_at:
                    raise TooLate()
                await asyncio.sleep(wait)


LIMITER = RateLimiter(TD_RPM, TD_DAILY_LIMIT)


# ---- Twelve Data ----
_client = None


def _http():
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=15)
    return _client


async def td_candles(symbol, interval, give_up_at=None):
    """Fetch 80 candles (oldest -> newest). Every call goes through the rate limiter first."""
    await LIMITER.acquire(give_up_at)
    resp = await _http().get("https://api.twelvedata.com/time_series", params={
        "symbol": symbol, "interval": interval, "outputsize": 80, "timezone": "UTC", "apikey": TD_KEY})
    data = resp.json()
    if data.get("status") == "error" or "values" not in data:
        code = data.get("code")
        if code == 429:
            daily = "day" in str(data.get("message", "")).lower()
            LIMITER.penalize(daily=daily)
            raise RateLimited("daily budget" if daily else "per-minute limit")
        raise RuntimeError(f"Twelve Data error code {code}")   # code only: the text could echo request details
    out = []
    for v in reversed(data["values"]):  # API gives newest first
        out.append({"t": datetime.strptime(v["datetime"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc),
                    "o": float(v["open"]), "h": float(v["high"]), "l": float(v["low"]), "c": float(v["close"])})
    return out


def closed_only(candles, step, now):
    """Drop the candle that is still forming (its signals would repaint)."""
    return [c for c in candles if c["t"].timestamp() + step <= now]


def due_boundary(now, tf, last):
    """Close time of the newest finished candle if it has not been scheduled yet and FETCH_OFFSET has passed."""
    step = TF_SECONDS[tf]
    b = int(now // step) * step
    if b == last or now - b < FETCH_OFFSET:
        return None
    return b


# ---- engine state helpers ----
def engine_on(app):
    return bool(app.bot_data.get("auto_on")) and bool(TD_KEY)


def _stats(app):
    s = app.bot_data.setdefault("stats", {"scans": 0, "queued": 0, "stale": 0, "msgs": 0, "last_signal": None,
                                          "last_scan": {}, "day": None, "today": 0})
    today = datetime.now(timezone.utc).date().isoformat()
    if s["day"] != today:
        s["day"], s["today"] = today, 0
    return s


def _drain(app):
    q = app.bot_data.get("jobq")
    while q is not None and not q.empty():
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            break


async def alert_admins(app, text):
    try:
        await bot_main.notify_admins(app.bot, app.bot_data["settings"], text)
    except Exception as exc:  # noqa: BLE001
        log.warning("admin alert failed: %s", type(exc).__name__)


async def _on_daily_limit(app):
    d = app.bot_data
    today = datetime.now(timezone.utc).date().isoformat()
    _drain(app)
    if d.get("daily_alert") == today:
        return
    d["daily_alert"] = today
    log.warning("daily API budget used: engine paused until 00:00 UTC")
    await alert_admins(app, f"⚠️ Twelve Data daily API budget used ({LIMITER.used_today()} requests). "
                            "Scanning is paused until 00:00 UTC. Use fewer pairs or a bigger Twelve Data plan "
                            "(then set TD_RPM / TD_DAILY_LIMIT in Render).")


# ---- analysis of one pair + timeframe ----
async def check(app, pair, tf, expected_open=None, give_up_at=None, force=False):
    """Fetch the latest CLOSED candles, evaluate, apply every filter. Returns (setup_or_None, note)."""
    d, step, label = app.bot_data, TF_SECONDS[tf], f"{pair} {TF_LABEL[tf]}"
    for attempt in (0, 1):
        candles = closed_only(await td_candles(pair, tf, give_up_at), step, _now())
        if not candles:
            return None, f"{label}: no closed candles"
        if force or expected_open is None or attempt or candles[-1]["t"].timestamp() >= expected_open:
            break
        await asyncio.sleep(RETRY_WAIT)     # provider is a few seconds late: one retry
    t_ts, now = int(candles[-1]["t"].timestamp()), _now()
    delay = now - (t_ts + step)
    if delay / 60 > STALE_MIN[tf]:
        if delay / 60 > 120:
            d.setdefault("closed_until", {})[pair] = now + 600     # market closed: stop spending credits on it
            return None, f"{label}: market closed (last candle {delay / 3600:.1f} h old)"
        return None, f"{label}: no fresh candles ({delay / 60:.0f} min old)"
    seen = d.setdefault("seen", {})
    if not force and seen.get((pair, tf)) == t_ts:
        return None, f"{label}: candle already analysed"
    seen[(pair, tf)] = t_ts
    if not force and delay > max_delay(tf):
        _stats(app)["stale"] += 1
        return None, f"{label}: stale ({delay:.0f}s after candle close), skipped"
    res = evaluate(candles)
    if res is None:
        return None, f"{label}: not enough data"
    if tf == "5min":
        d.setdefault("htf", {})[pair] = (res["direction"], res["score"], now)
    tag = " (flat market)" if res["flat"] else ""
    note = f"{label}: score {res['score']:+d}{tag}"
    if res["flat"] or abs(res["score"]) < MIN_SCORE_TF[tf]:
        return None, note
    if not force and now - d.setdefault("cool", {}).get((pair, tf), 0) < COOLDOWN_MIN[tf] * 60:
        return None, note + " (cooldown)"
    aligned = False
    if tf == "1min":
        h = d.get("htf", {}).get(pair)
        aligned = bool(h and h[0] == res["direction"] and abs(h[1]) >= 2 and now - h[2] < 720)
        if REQUIRE_ALIGN and "5min" in TIMEFRAMES and not aligned and not force:
            return None, note + " (5m trend not aligned)"
    return {**res, "pair": pair, "tf": tf, "t": t_ts, "candles": candles, "aligned": aligned}, \
        note + f" -> {res['direction']}"


async def safe_check(app, pair, tf, expected_open=None, give_up_at=None, force=False):
    label = f"{pair} {TF_LABEL[tf]}"
    try:
        return await check(app, pair, tf, expected_open, give_up_at, force)
    except TooLate:
        return None, f"{label}: skipped (no free API slot in time)"
    except DailyLimit:
        await _on_daily_limit(app)
        return None, f"{label}: daily API budget used"
    except RateLimited as exc:
        return None, f"{label}: API rate limit hit ({exc})"
    except RuntimeError as exc:
        return None, f"{label}: {exc}"
    except Exception as exc:  # noqa: BLE001 - type only (error text may hold the API key)
        return None, f"{label}: data error ({type(exc).__name__})"


async def dispatch(app, s, force=False):
    """Duplicate check -> chart -> queue for sending. Returns True when queued."""
    d, pair, tf = app.bot_data, s["pair"], s["tf"]
    close_ts = s["t"] + TF_SECONDS[tf]
    if not force:
        rounds = d.setdefault("rounds", {})
        if MAX_PER_ROUND and rounds.get((tf, close_ts), 0) >= MAX_PER_ROUND:
            return False
        if not await claim_signal(app, f"{pair}|{tf}|{s['t']}", pair, tf, s["direction"], s["score"]):
            log.info("duplicate signal blocked: %s %s", pair, TF_LABEL[tf])
            return False
        rounds[(tf, close_ts)] = rounds.get((tf, close_ts), 0) + 1
        if len(rounds) > 200:
            for k in sorted(rounds)[:-100]:
                rounds.pop(k, None)
    d.setdefault("cool", {})[(pair, tf)] = _now()
    png = await asyncio.to_thread(render_chart, s["candles"], s["direction"],
                                  f"{pair} {TF_LABEL[tf]}  {TZ_LABEL}", UTC_OFFSET_MIN)
    caption = build_caption(pair, tf, s, entry_time(_now()), aligned=s.get("aligned", False))
    await d["sendq"].put((caption, png, f"{pair} {TF_LABEL[tf]} {s['direction']}"))
    st = _stats(app)
    st["queued"] += 1
    st["today"] += 1
    st["last_signal"] = (pair, tf, s["direction"], _now())
    return True


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


# ---- the three engine loops ----
async def scheduler_loop(app):
    """Every second: if a 1m / 5m candle just closed, queue one job per pair (5m first, it is the rarer one)."""
    d, last, seq, hb = app.bot_data, {}, itertools.count(), _now()
    while True:
        await asyncio.sleep(1)
        now = _now()
        if now - hb >= 300:
            hb = now
            st = _stats(app)
            log.info("engine heartbeat: %s | scans=%d signals_today=%d stale=%d | API last min=%d today=%d",
                     "ON" if engine_on(app) else "OFF", st["scans"], st["today"], st["stale"],
                     LIMITER.used_recent(), LIMITER.used_today())
        if not engine_on(app):
            continue
        for tf in TIMEFRAMES:
            b = due_boundary(now, tf, last.get(tf))
            if b is None:
                continue
            last[tf] = b
            if now - b > max_delay(tf):
                continue                # engine was just started late in this candle: wait for the next one
            for pair in PAIRS:
                d["jobq"].put_nowait((-TF_SECONDS[tf], b, next(seq), pair, tf))


async def worker_loop(app):
    """Takes one job at a time; the rate limiter decides when each API call may go out."""
    d = app.bot_data
    while True:
        _prio, boundary, _n, pair, tf = await d["jobq"].get()
        if not engine_on(app) or LIMITER.day_exhausted():
            continue
        now = _now()
        if now - boundary > max_delay(tf) or d.get("closed_until", {}).get(pair, 0) > now:
            if now - boundary > max_delay(tf):
                _stats(app)["stale"] += 1
            continue
        if d.get("seen", {}).get((pair, tf)) == boundary - TF_SECONDS[tf]:
            continue                    # this candle was already analysed: do not spend an API request on it
        setup, note = await safe_check(app, pair, tf, expected_open=boundary - TF_SECONDS[tf],
                                       give_up_at=boundary + max_delay(tf) - 3)
        st = _stats(app)
        st["scans"] += 1
        st["last_scan"][tf] = _now()
        d.setdefault("notes", {})[(pair, tf)] = (_now(), note)
        log.debug("scan: %s", note)
        if setup:
            try:
                await dispatch(app, setup)
            except Exception as exc:  # noqa: BLE001
                log.error("dispatch failed: %s", type(exc).__name__)


async def sender_loop(app):
    """One sender for everything, so several signals can never push Telegram past its message limit."""
    q = app.bot_data["sendq"]
    while True:
        caption, png, label = await q.get()
        try:
            sent, total = await send_signal(app, caption, png)
            _stats(app)["msgs"] += sent
            log.info("SENT %s to %d/%d users", label, sent, total)
        except Exception as exc:  # noqa: BLE001
            log.error("sending %s failed: %s", label, type(exc).__name__)


async def keepalive_loop(app):
    url = app.bot_data["settings"].webhook_base_url
    if not url:
        return
    while True:
        await asyncio.sleep(600)
        try:
            await _http().get(url + "/health")
        except Exception:  # noqa: BLE001
            pass


async def _forever(name, fn, app):
    while True:
        try:
            await fn(app)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("engine loop %s crashed (%s), restarting", name, type(exc).__name__)
            await asyncio.sleep(5)


async def signal_engine(app):
    while app.bot_data.get("store") is None or not app.running:  # wait until the bot is fully started
        await asyncio.sleep(2)
    d = app.bot_data
    d.setdefault("jobq", asyncio.PriorityQueue())
    d.setdefault("sendq", asyncio.Queue())
    d.setdefault("cool", {})
    if not TD_KEY:
        d["auto_on"] = False
        log.warning("TWELVE_DATA_API_KEY is not set: automatic signals are disabled")
        return
    await load_cooldowns(app)
    saved = await load_engine_state(app)
    d["auto_on"] = AUTOSTART if saved is None else saved
    loops = [("scheduler", scheduler_loop), ("worker", worker_loop), ("sender", sender_loop)]
    if KEEPALIVE:
        loops.append(("keepalive", keepalive_loop))
    d["engine_tasks"] = [asyncio.get_running_loop().create_task(_forever(n, f, app)) for n, f in loops]
    d["engine_started"] = True
    log.info("signal engine ready: %d pairs | timeframes %s | API %d/min, %s/day | engine %s",
             len(PAIRS), ",".join(TF_LABEL[t] for t in TIMEFRAMES), TD_RPM,
             TD_DAILY_LIMIT or "unlimited", "ON" if d["auto_on"] else "OFF")


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


async def welcome_entry(update, context):
    """/start and /help: the original welcome, plus a short engine hint for admins only."""
    await welcome(update, context)
    if bot_main._is_admin(update, context):
        await update.effective_message.reply_text("Admin: /engine = START / STOP the signal engine + status.")


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


def _tf_arg(raw):
    return _TF_ALIAS.get((raw or "").strip().lower())


# ---- engine control panel (admin only) ----
ENGINE_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("▶️ START ENGINE", callback_data="eng:start"),
     InlineKeyboardButton("⏹ STOP ENGINE", callback_data="eng:stop")],
    [InlineKeyboardButton("🔄 Refresh status", callback_data="eng:status")],
])


def engine_label(app):
    if not TD_KEY:
        return "NO API KEY ⚠️"
    if LIMITER.day_exhausted():
        return "PAUSED - daily API budget used ⏸ (resumes 00:00 UTC)"
    return "RUNNING 🟢" if app.bot_data.get("auto_on") else "STOPPED 🔴"


async def status_text(app):
    d, st = app.bot_data, _stats(app)
    approved = set(await d["store"].list_approved_ids())
    stopped = len(approved & await optouts(app))
    need = len(PAIRS) * sum(60 / TF_SECONDS[t] for t in TIMEFRAMES)      # requests per minute needed
    lines = [f"⚙️ SIGNAL ENGINE: {engine_label(app)}",
             f"Pairs ({len(PAIRS)}): {', '.join(PAIRS)}",
             "Timeframes: " + " | ".join(f"{TF_LABEL[t]} (expiry {EXPIRY[t]} min, min {MIN_SCORE_TF[t]}/5, "
                                         f"cooldown {COOLDOWN_MIN[t]} min)" for t in TIMEFRAMES),
             f"API: needs ~{need:.1f} requests/min | limit {TD_RPM}/min | used last minute {LIMITER.used_recent()} | "
             f"today {LIMITER.used_today()}" + (f"/{TD_DAILY_LIMIT}" if TD_DAILY_LIMIT else ""),
             f"Scans: {st['scans']} | stale skipped: {st['stale']} | signals today: {st['today']} | "
             f"messages sent: {st['msgs']}"]
    if need > TD_RPM:
        lines.append(f"⚠️ {len(PAIRS)} pairs need more than {TD_RPM} requests/min: some scans will be skipped. "
                     "Use fewer pairs or raise TD_RPM with a bigger Twelve Data plan.")
    if TD_DAILY_LIMIT and need * 60 * 24 > TD_DAILY_LIMIT:
        lines.append(f"⚠️ At this rate the {TD_DAILY_LIMIT}/day budget lasts about "
                     f"{TD_DAILY_LIMIT / need / 60:.1f} hours per day.")
    if st["last_signal"]:
        p, tf, dr, ts = st["last_signal"]
        lines.append(f"Last signal: {p} {TF_LABEL[tf]} {dr} at {datetime.fromtimestamp(ts, timezone.utc):%H:%M:%S} UTC")
    lines.append(f"Approved users: {len(approved)} (stopped: {stopped}) | min. align 1M+5M: "
                 f"{'required' if REQUIRE_ALIGN else 'label only'}")
    return "\n".join(lines)


async def set_engine(app, on):
    if not TD_KEY:
        return "Add TWELVE_DATA_API_KEY in Render -> Environment first."
    if not app.bot_data.get("engine_started"):
        return "The engine is still starting. Try again in a few seconds."
    app.bot_data["auto_on"] = on
    if not on:
        _drain(app)
    await save_engine_state(app, on)
    return ("▶️ Engine STARTED. Scanning every closed " + " and ".join(TF_LABEL[t] for t in TIMEFRAMES) + " candle."
            if on else "⏹ Engine STOPPED. No new signals will be sent until you press START ENGINE.")


async def engine_cmd(update, context):
    if not bot_main._is_admin(update, context):
        return
    await update.effective_message.reply_text(await status_text(context.application), reply_markup=ENGINE_KB)


async def startengine_cmd(update, context):
    if not bot_main._is_admin(update, context):
        return
    await update.effective_message.reply_text(await set_engine(context.application, True), reply_markup=ENGINE_KB)


async def stopengine_cmd(update, context):
    if not bot_main._is_admin(update, context):
        return
    await update.effective_message.reply_text(await set_engine(context.application, False), reply_markup=ENGINE_KB)


async def on_engine_button(update, context):
    q = update.callback_query
    app = context.application
    chat_id = q.message.chat.id if q.message else None
    if not app.bot_data["settings"].is_admin(q.from_user.id, chat_id):
        await q.answer("Only admins can do this.", show_alert=True)
        return
    action = q.data.split(":", 1)[1]
    if action == "status":
        await q.answer("Refreshed.")
    else:
        msg = await set_engine(app, action == "start")
        await q.answer(msg[:190], show_alert=not msg.startswith(("▶️", "⏹")))
    try:
        await q.edit_message_text(await status_text(app), reply_markup=ENGINE_KB)
    except Exception:  # noqa: BLE001 - "message is not modified" when nothing changed
        pass


async def autosignal_cmd(update, context):
    if not bot_main._is_admin(update, context):
        return
    app, msg = context.application, update.effective_message
    args = context.args or []
    arg = args[0].lower() if args else ""
    if arg in ("on", "off"):
        await msg.reply_text(await set_engine(app, arg == "on"), reply_markup=ENGINE_KB)
        return
    if arg in ("now", "test"):
        if not TD_KEY:
            await msg.reply_text("Add TWELVE_DATA_API_KEY in Render -> Environment first.")
            return
        if not app.bot_data.get("engine_started"):
            await msg.reply_text("The engine is still starting. Try again in a few seconds.")
            return
        chat_id, uid = msg.chat_id, update.effective_user.id
        if arg == "now":
            jobs = len(PAIRS) * len(TIMEFRAMES)
            await msg.reply_text(f"Scanning {len(PAIRS)} pairs x {len(TIMEFRAMES)} timeframes ({jobs} requests, "
                                 f"about {jobs / TD_RPM + 0.5:.0f} min at {TD_RPM}/min)...")

            async def job():
                notes, queued = [], 0
                for tf in TIMEFRAMES:
                    for pair in PAIRS:
                        setup, note = await safe_check(app, pair, tf, force=True)
                        notes.append(note)
                        if setup and await dispatch(app, setup, force=True):
                            queued += 1
                notes.append(f"{queued} signal(s) queued for approved users." if queued
                             else "No setup strong enough right now (nothing sent).")
                await app.bot.send_message(chat_id, "Scan result:\n" + "\n".join(notes))
        else:
            pair = _pair_arg(args[1]) if len(args) > 1 else PAIRS[0]
            tf = _tf_arg(args[2]) if len(args) > 2 else TIMEFRAMES[0]
            if pair is None or tf is None:
                await msg.reply_text("Usage: /autosignal test EURUSD [1m|5m]")
                return
            await msg.reply_text(f"Building a test signal for {pair} {TF_LABEL[tf]}...")

            async def job():
                try:
                    candles = closed_only(await td_candles(pair, tf), TF_SECONDS[tf], _now())
                except Exception as exc:  # noqa: BLE001
                    await app.bot.send_message(chat_id, f"Could not get data ({type(exc).__name__}).")
                    return
                res = evaluate(candles)
                if res is None:
                    await app.bot.send_message(chat_id, "Not enough data for a test signal.")
                    return
                png = await asyncio.to_thread(render_chart, candles, res["direction"],
                                              f"{pair} {TF_LABEL[tf]}  {TZ_LABEL}", UTC_OFFSET_MIN)
                await send_signal(app, build_caption(pair, tf, res, entry_time(_now()), test=True), png, only=[uid])

        _spawn(app, job())
        return
    await msg.reply_text(await status_text(app) + "\n\n/engine   /startengine   /stopengine\n"
                         "/autosignal now   /autosignal test EURUSD [1m|5m]", reply_markup=ENGINE_KB)


# ---- hook everything into main-4.py ----
_orig_build = bot_main.build_application
_orig_make_http = bot_main.make_http_app
_orig_approval = bot_main.approval_text


def _build(settings, polling):
    app = _orig_build(settings, polling)
    private = filters.ChatType.PRIVATE
    app.add_handler(CallbackQueryHandler(on_welcome_button, pattern=r"^w:(join|new|group|help|old)$"))
    app.add_handler(CallbackQueryHandler(on_sig_button, pattern=r"^sig:(stop|start)$"))
    app.add_handler(CallbackQueryHandler(on_engine_button, pattern=r"^eng:(start|stop|status)$"))
    app.add_handler(CommandHandler("stopsignals", stop_cmd, filters=private))
    app.add_handler(CommandHandler("startsignals", start_signals_cmd, filters=private))
    app.add_handler(CommandHandler("autosignal", autosignal_cmd))
    app.add_handler(CommandHandler("engine", engine_cmd))
    app.add_handler(CommandHandler("startengine", startengine_cmd))
    app.add_handler(CommandHandler("stopengine", stopengine_cmd))
    return app


def _make_http(ctx):
    asyncio.get_running_loop().create_task(signal_engine(ctx.ptb))  # runs inside the bot's event loop
    return _orig_make_http(ctx)


bot_main.start = welcome_entry  # /start and /help use the new welcome
bot_main.build_application = _build
bot_main.make_http_app = _make_http
bot_main.approval_text = lambda s, row: _orig_approval(s, row) + APPROVED_NOTE

if __name__ == "__main__":
    bot_main.main()
