"""HONESTY AI VIP BOT - MASTER UPDATE (loads run.py + main-4.py unchanged and adds features on top).
Render start command:  python master.py        (old command `python run.py` still works = instant rollback)

Adds:
  /admin            admin dashboard (inline buttons)
  /forwardchannel   connect / test / replace / disconnect a Telegram channel; every real signal is auto-posted there
  /edit             edit settings from buttons (validated, saved, applied live without restart)
  promotions        one promo message to the channel every N hours (default 5), survives restarts, no duplicates
  /restartengine /scan /assets /signals /stats
  signal header     "🔷 HONESTY AI VIP BOT" -> "❤️‍🔥 HONESTY AI VIP BOT" (editable in /edit)
Existing commands (/engine /startengine /stopengine /autosignal ...) stay exactly as they are.
Settings are stored in PostgreSQL (table bot_engine_state, key master_cfg). Without DATABASE_URL they reset on restart.
"""
import asyncio
import html
import importlib.util
import json
import math
import os
import threading
from decimal import Decimal
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as KB, InputFile
from telegram.error import RetryAfter, TelegramError
from telegram.ext import ExtBot
from telegram.ext import (ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, MessageHandler, filters)

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("run_mod", HERE / "run.py")
run = importlib.util.module_from_spec(_spec)
sys.modules["run_mod"] = run
_spec.loader.exec_module(run)          # run.py hooks itself into main-4.py on import
bot_main = run.bot_main
log = run.log
STARTED = time.time()

DEFAULT_PROMO = """🔥 <b>WELCOME TO OUR ADVANCED SIGNAL COMMUNITY!</b>

🚀 <b>What You Get:</b>

📊 Multi-Pair Market Analysis
📈 CALL &amp; PUT Trading Signals
🎯 Multiple Indicator Confirmations (EMA, RSI, MACD, price action)
⚡ Signals on every closed 1M / 5M candle
📡 Automatic Signal Updates

👇 <b>Join Our Trading Community</b>

🔗 My Referral Link:
{referral}

⚠️ Trade responsibly. No signal is guaranteed."""

CFG = {"channel_id": None, "channel_title": "", "auto_forward": True, "last_forward": "never",
       "promo_on": True, "promo_hours": 5.0, "promo_text": DEFAULT_PROMO, "last_promo": 0.0,
       "referral": "https://broker-qx.pro/sign-up/?lid=2432286",
       "header": "❤️‍🔥 HONESTY TAMIL",
       "disclaimer": "", "strict": True, "min_deposit": 40, "min_score": 70, "vals": {}}


# ----------------------------- persistence -----------------------------
async def cfg_load(app):
    pool = run._pool(app)
    if pool is None:
        log.warning("master: no DATABASE_URL, settings will reset on restart")
        return
    try:
        await run._ensure_table(app)
        row = await pool.fetchrow("SELECT v FROM bot_engine_state WHERE k='master_cfg'")
        if row:
            CFG.update(json.loads(row["v"]))
    except Exception as exc:  # noqa: BLE001
        log.warning("master: could not load settings (%s)", type(exc).__name__)


async def cfg_save(app):
    pool = run._pool(app)
    if pool is None:
        return
    try:
        await run._ensure_table(app)
        await pool.execute("INSERT INTO bot_engine_state (k, v) VALUES ('master_cfg', $1) "
                           "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v", json.dumps(CFG))
    except Exception as exc:  # noqa: BLE001
        log.warning("master: could not save settings (%s)", type(exc).__name__)


# ----------------------------- editable settings -----------------------------
def v_int(lo, hi):
    def f(s):
        try:
            n = int(s.strip())
        except ValueError:
            raise ValueError("Send a whole number.")
        if not lo <= n <= hi:
            raise ValueError(f"Must be between {lo} and {hi}.")
        return n
    return f


def v_float(lo, hi):
    def f(s):
        try:
            n = float(s.strip())
        except ValueError:
            raise ValueError("Send a number.")
        if not lo <= n <= hi:
            raise ValueError(f"Must be between {lo} and {hi}.")
        return n
    return f


def v_text(maxlen, must=None):
    def f(s):
        s = s.strip()
        if not s or len(s) > maxlen:
            raise ValueError(f"Text must be 1-{maxlen} characters.")
        if must and must not in s:
            raise ValueError(f"Text must contain {must}")
        return s
    return f


def v_disc(s):
    s = s.strip()
    if s == "-":
        return ""                      # send "-" to remove the disclaimer completely
    if len(s) > 300:
        raise ValueError("Text must be at most 300 characters (or send - to remove it).")
    return s


def v_url(s):
    s = s.strip()
    if not s.startswith(("https://", "http://")) or " " in s or len(s) > 300:
        raise ValueError("Send a full link starting with https://")
    return s


ALL_PAIRS = ("EUR/USD GBP/USD USD/JPY USD/CHF USD/CAD AUD/USD NZD/USD EUR/GBP EUR/JPY EUR/CHF EUR/CAD EUR/AUD EUR/NZD "
             "GBP/JPY GBP/CHF GBP/CAD GBP/AUD GBP/NZD AUD/JPY AUD/CHF AUD/CAD AUD/NZD CAD/JPY CAD/CHF CHF/JPY NZD/JPY NZD/CAD "
             "NZD/CHF").split()          # the 28 core forex pairs; exotic / crypto / metals only if you add them yourself


def v_pairs(s):
    if s.strip().upper() == "ALL":
        return list(ALL_PAIRS)
    p = run._pairs(s)
    if not p or len(p) > 120:
        raise ValueError("Send ALL, or pairs like: EUR/USD,GBP/USD,BTC/USD")
    return p


def v_tfs(s):
    t = run._timeframes(s)
    if not t:
        raise ValueError("Send 1m, 5m or 1m,5m")
    return t


def _setkey(d, k):
    return lambda v: d.__setitem__(k, v)


def _setrun(name):
    return lambda v: setattr(run, name, v)


def _setlist(lst):
    def f(v):
        lst[:] = v
    return f


# key: (label, kind, getter, setter, validator)    kind: val | text | bool
SETTINGS = {
    "header": ("Signal header", "text", lambda: CFG["header"], _setkey(CFG, "header"), v_text(60)),
    "min_score": ("Min analysis score (50-95)", "val", lambda: CFG["min_score"], _setkey(CFG, "min_score"), v_int(50, 95)),
    "pairs": ("Regular pairs (send ALL for the 28 core pairs)", "val", lambda: ",".join(run.PAIRS), _setlist(run.PAIRS), v_pairs),
    "cool1": ("Cooldown per pair (min)", "val", lambda: run.COOLDOWN_MIN["1min"], _setkey(run.COOLDOWN_MIN, "1min"), v_int(0, 240)),
    "auto_forward": ("Auto-forward signals to channel", "bool", lambda: CFG["auto_forward"], _setkey(CFG, "auto_forward"), None),
    "strict": ("Strict Trader ID check (postback)", "bool", lambda: CFG["strict"], _setkey(CFG, "strict"), None),
    "min_deposit": ("Min deposit USD (used if MIN_DEPOSIT env is empty)", "val", lambda: CFG["min_deposit"], _setkey(CFG, "min_deposit"), v_float(1, 100000)),
    "promo_on": ("Promotional messages", "bool", lambda: CFG["promo_on"], _setkey(CFG, "promo_on"), None),
    "promo_hours": ("Promo interval (hours)", "val", lambda: CFG["promo_hours"], _setkey(CFG, "promo_hours"), v_float(1, 168)),
    "promo_text": ("Promo message (HTML)", "text", lambda: CFG["promo_text"], _setkey(CFG, "promo_text"), v_text(3500, "{referral}")),
    "referral": ("Referral link", "text", lambda: CFG["referral"], _setkey(CFG, "referral"), v_url),
    "welcome": ("Bot welcome message", "text", lambda: run.WELCOME_TEXT, _setrun("WELCOME_TEXT"), v_text(900, "{first_name}")),
}
BOOL_PANEL = {"auto_forward": "fwd", "promo_on": "promo"}


def show(key):
    v = SETTINGS[key][2]()
    if SETTINGS[key][1] == "bool":
        return "ON ✅" if v else "OFF ❌"
    v = str(v).replace("\n", " ")
    return v if len(v) <= 28 else v[:28] + "…"


def apply_saved():
    for k, v in list(CFG.get("vals", {}).items()):
        if k in SETTINGS:
            try:
                SETTINGS[k][3](v)
            except Exception as exc:  # noqa: BLE001
                log.warning("master: could not apply saved %s (%s)", k, type(exc).__name__)


# ----------------------------- channel + promo -----------------------------
async def connect_channel(app, raw):
    raw = raw.strip()
    ref = int(raw) if raw.lstrip("-").isdigit() else (raw if raw.startswith("@") else "@" + raw)
    try:
        chat = await app.bot.get_chat(ref)
        if chat.type != "channel":
            raise ValueError("That chat is not a channel.")
        me = await app.bot.get_chat_member(chat.id, app.bot.id)
        if me.status != "administrator":
            raise ValueError("The bot is not an administrator of that channel. Add it as admin first.")
        if getattr(me, "can_post_messages", False) is not True:
            raise ValueError("The bot is admin but has no permission to post messages.")
        await app.bot.send_message(chat.id, "✅ Signal bot connected. Signals will be posted here.")
    except TelegramError as exc:
        raise ValueError(f"Telegram could not access that channel ({type(exc).__name__}). "
                         "Check the ID / @username and that the bot is admin.")
    CFG.update(channel_id=chat.id, channel_title=chat.title or str(chat.id), last_forward="never")
    if not CFG["last_promo"]:
        CFG["last_promo"] = time.time()      # first promo comes after one full interval
    await cfg_save(app)
    return chat.title or str(chat.id)


def promo_html():
    return CFG["promo_text"].replace("{referral}", html.escape(CFG["referral"]))


async def send_promo(app):
    if not CFG["channel_id"]:
        return "No channel connected."
    try:
        await app.bot.send_message(CFG["channel_id"], promo_html(), parse_mode="HTML",
                                   link_preview_options=run.NO_PREVIEW)
        return "✅ Promotional message sent."
    except TelegramError as exc:
        return f"❌ Promo failed: {type(exc).__name__}: {str(exc)[:120]}"


async def promo_loop(app):
    while True:
        await asyncio.sleep(60)
        try:
            if CFG["promo_on"] and CFG["channel_id"] and time.time() - CFG["last_promo"] >= CFG["promo_hours"] * 3600:
                CFG["last_promo"] = time.time()   # saved BEFORE sending: a restart can never double-send
                await cfg_save(app)
                log.info("master promo: %s", await send_promo(app))
        except Exception as exc:  # noqa: BLE001
            log.error("master promo loop error: %s", type(exc).__name__)


async def send_signal(app, caption, png, only=None):
    """Same as run.send_signal but the caption is sent as HTML (clickable REGISTER link); also posts to the channel."""
    if only is None:
        approved = set(await app.bot_data["store"].list_approved_ids())
        ids = (approved | set(app.bot_data["settings"].admin_ids)) - await run.optouts(app)
    else:
        ids = set(only)
    file_id, sent = None, 0
    for uid in sorted(ids):
        for _ in range(2):
            try:
                m = await app.bot.send_photo(uid, file_id or InputFile(png, filename="signal.png"), caption=caption,
                                             parse_mode="HTML", reply_markup=run.STOP_KB)
                file_id = file_id or m.photo[-1].file_id
                sent += 1
                break
            except RetryAfter as exc:
                ra = exc.retry_after
                await asyncio.sleep((ra.total_seconds() if hasattr(ra, "total_seconds") else ra) + 1)
            except Exception as exc:  # noqa: BLE001
                log.warning("signal not delivered to %s: %s", uid, type(exc).__name__)
                break
        await asyncio.sleep(0.05)
    if only is None and CFG["auto_forward"] and CFG["channel_id"]:    # real signals only, never test signals
        try:
            await app.bot.send_photo(CFG["channel_id"], file_id or InputFile(png, filename="signal.png"),
                                     caption=caption, parse_mode="HTML")
            CFG["last_forward"] = "OK " + datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        except Exception as exc:  # noqa: BLE001
            CFG["last_forward"] = f"FAILED ({type(exc).__name__})"
            log.warning("master: channel forward failed: %s", type(exc).__name__)
    return sent, len(ids)


_orig_caption = run.build_caption          # the original message from run.py (analysis text etc. stays unchanged)


_FIELD_RE = re.compile(r"^\W*(Asset|Entry Time|Expiry|Signal|Analysis)\s*:\s*(.*)$")


def _short_analysis(t):
    """One short line built only from what the analysis really found (no invented facts)."""
    if t.startswith("SHORT:"):
        return t[6:].strip()
    parts = []
    if "EMA 9 is above EMA 21" in t:
        parts.append("EMA 9 above EMA 21")
    elif "EMA 9 is below EMA 21" in t:
        parts.append("EMA 9 below EMA 21")
    if "MACD momentum is rising" in t:
        parts.append("MACD rising")
    elif "MACD momentum is falling" in t:
        parts.append("MACD falling")
    m = re.search(r"RSI at (\d+)", t)
    if m:
        parts.append(f"RSI {m.group(1)}")
    return ", ".join(parts) or t[:90]


def build_caption(pair, tf, res, entry_ts, aligned=False, test=False):
    """Exactly 5 lines: Asset, Entry Time, Expiry, Signal, AI Analysis (no header, no strength, no disclaimer)."""
    text = _orig_caption(pair, tf, res, entry_ts, aligned=aligned, test=test)
    f = {}
    for ln in text.split("\n"):
        m = _FIELD_RE.match(ln)
        if m and m.group(1) not in f:
            f[m.group(1)] = m.group(2).strip()
    if not all(k in f for k in ("Asset", "Entry Time", "Expiry", "Signal")):
        return "<b>" + html.escape(text) + "</b>"     # unknown layout: never lose a signal
    lines = (["🧪 TEST SIGNAL (only you can see this)"] if test else []) + [CFG["header"],
        f"📊 Asset: {f['Asset']}", f"🕐 Entry Time: {f['Entry Time'].replace(' IST', '').strip()}",
        f"⏳ Expiry: {f['Expiry']}", f"🎯 Signal: {f['Signal']}",
        f"🧠 AI Analysis: {_short_analysis(f.get('Analysis', ''))}"]
    return "<b>" + html.escape("\n".join(lines)) + "</b>"


run.send_signal = send_signal
run.build_caption = build_caption


# ----------------------------- text helpers -----------------------------
def ist(ts):
    return (ts + timedelta(minutes=run.UTC_OFFSET_MIN)).strftime("%d %b %H:%M")


async def stats_text(app):
    st, pool = run._stats(app), run._pool(app)
    lines = ["📈 SIGNAL STATISTICS"]
    outcome = "Win/loss: needs PostgreSQL to record results."
    if pool is not None:
        try:
            await run._ensure_table(app)
            total = await pool.fetchval("SELECT count(*) FROM bot_signal_log")
            d = {r["direction"]: r["c"] for r in await pool.fetch(
                "SELECT direction, count(*) AS c FROM bot_signal_log GROUP BY direction")}
            pairs = await pool.fetch("SELECT pair, count(*) AS c FROM bot_signal_log GROUP BY pair ORDER BY c DESC")
            day = await pool.fetchval("SELECT count(*) FROM bot_signal_log WHERE created_at > now() - interval '24 hours'")
            lines += [f"Total signals: {total} (last 24h: {day})", f"CALL: {d.get('CALL', 0)} | PUT: {d.get('PUT', 0)}",
                      "Per pair: " + (", ".join(f"{r['pair']} {r['c']}" for r in pairs) or "-")]
            await ensure_results(pool)
            w = await pool.fetchval("SELECT count(*) FROM master_results WHERE r='W'")
            lo = await pool.fetchval("SELECT count(*) FROM master_results WHERE r='L'")
            done = w + lo
            outcome = f"Recorded results (you tapped them): ✅ {w} | ❌ {lo} | unrecorded {max(total - done, 0)}"
            if done:
                outcome += f"\nWin rate: {w * 100 / done:.0f}% of {done} recorded"
                if done < 30:
                    outcome += " (too few trades, need 30+ to trust it)"
        except Exception as exc:  # noqa: BLE001
            lines.append(f"(database stats unavailable: {type(exc).__name__})")
    else:
        lines.append("No database: showing this session only.")
    last = max(st["last_scan"].values()) if st["last_scan"] else None
    lines += [f"Signals today: {st['today']} | scans: {st['scans']} | stale skipped: {st['stale']}",
              f"Last scan: {datetime.fromtimestamp(last, timezone.utc):%H:%M:%S} UTC" if last else "Last scan: none yet",
              f"Uptime: {timedelta(seconds=int(time.time() - STARTED))}",
              outcome,
              "Rejected weak setups are not counted."]
    return "\n".join(lines)


async def status_all(app):
    ch = f"🟢 {CFG['channel_title']}" if CFG["channel_id"] else "🔴 none"
    db = "PostgreSQL ✅" if run._pool(app) is not None else "memory only ⚠️ (settings reset on restart)"
    nxt = ""
    if CFG["promo_on"] and CFG["channel_id"]:
        nxt = f"\nNext promo: {ist(datetime.fromtimestamp(CFG['last_promo'] + CFG['promo_hours'] * 3600, timezone.utc))}"
    return (await run.status_text(app)) + f"\n\n📡 Channel: {ch} | forward {'ON' if CFG['auto_forward'] else 'OFF'}" \
        f"\n📢 Promo: {'ON' if CFG['promo_on'] else 'OFF'} every {CFG['promo_hours']:g} h{nxt}" \
        f"\n🗄 Storage: {db}\n⏱ Uptime: {timedelta(seconds=int(time.time() - STARTED))}"


# ----------------------------- panels -----------------------------
def kb(rows):
    return KB([[B(t, callback_data=c) for t, c in r] for r in rows])


BACK = [("⬅️ Back", "m:home")]
MENU = [("📡 Forward Channel", "fwd"), ("⚙️ Signal Engine", "eng"), ("🧠 Analysis Settings", "ai"),
        ("📊 Asset Management", "assets"), ("✏️ Edit Bot Settings", "edit"), ("📢 Channel Promotion", "promo"),
        ("⏱️ Auto Message Settings", "promo"), ("📈 Signal Statistics", "stats"), ("👥 User Management", "users"),
        ("🔄 Restart Engine", "restart"), ("🟢 Start Engine", "start"), ("🔴 Stop Engine", "stop"),
        ("📋 System Status", "status")]


def edit_rows(keys):
    return [[(f"{SETTINGS[k][0]}: {show(k)}", ("t:" if SETTINGS[k][1] == "bool" else "e:") + k)] for k in keys]


async def render(app, key):
    if key == "home":
        return "🛠 ADMIN DASHBOARD\nChoose an option:", kb([[(l, "m:" + k) for l, k in MENU[i:i + 2]]
                                                          for i in range(0, len(MENU), 2)])
    if key == "fwd":
        st = f"🟢 Connected: {CFG['channel_title']} ({CFG['channel_id']})" if CFG["channel_id"] else "🔴 Disconnected"
        return (f"📡 FORWARD CHANNEL\n{st}\nAuto-forward: {'ON' if CFG['auto_forward'] else 'OFF'}\n"
                f"Last forward: {CFG['last_forward']}"), kb([
            [("🔗 Set / replace channel", "f:set"), ("🧪 Send test", "f:test")],
            [(f"Auto-forward: {show('auto_forward')}", "t:auto_forward")],
            [("🔌 Disconnect", "f:off")], BACK])
    if key == "eng":
        rows = [[(b.text, b.callback_data) for b in r] for r in run.ENGINE_KB.inline_keyboard]
        return await status_all(app), kb(rows + [BACK])
    if key == "ai":
        return ("🧠 ANALYSIS SETTINGS\n2-minute signals only. Checked on every closed candle: EMA 9/21/50, RSI, MACD, "
                "Stochastic, ADX/DI, Supertrend, Bollinger, support/resistance, candle patterns, 5M trend.\n"
                "A signal is sent only when the weighted score reaches the minimum and the 5M trend agrees."), \
            kb(edit_rows(["min_score"]) + [BACK])
    if key == "assets":
        return (f"📊 ASSETS\nRegular pairs ({len(run.PAIRS)}): {', '.join(run.PAIRS)}\nOTC: {', '.join(otc_label(x) for x in OTC['symbols']) or OTC['status']}\n"
                "Signals: 2 minutes (1M candles)\nRegular pairs: Twelve Data. OTC pairs: OTCharts (needs a paid plan)."), kb(edit_rows(["pairs"]) + [BACK])
    if key == "edit":
        return "✏️ EDIT BOT SETTINGS\nTap a setting to change it. Changes apply immediately.", \
            kb(edit_rows(list(SETTINGS)) + [BACK])
    if key == "promo":
        return ("📢 CHANNEL PROMOTION\n" f"Status: {'ON' if CFG['promo_on'] else 'OFF'} | every {CFG['promo_hours']:g} h\n"
                f"Channel: {CFG['channel_title'] or 'not connected'}\nReferral: {CFG['referral']}"), kb([
            [(f"Promo: {show('promo_on')}", "t:promo_on")],
            [(f"Interval: {show('promo_hours')} h", "e:promo_hours")],
            [("✏️ Edit message", "e:promo_text"), ("🔗 Edit referral", "e:referral")],
            [("📤 Send now", "p:now")], BACK])
    if key == "stats":
        return await stats_text(app), kb([BACK])
    if key == "users":
        approved = set(await app.bot_data["store"].list_approved_ids())
        off = await run.optouts(app)
        return (f"👥 USERS\nApproved: {len(approved)}\nSignals stopped by user: {len(approved & off)}\n"
                f"Receiving signals: {len(approved - off)}"), kb([BACK])
    return await status_all(app), kb([BACK])      # "status"


def adm(update, context):
    u, c = update.effective_user, update.effective_chat
    return bool(u and context.application.bot_data["settings"].is_admin(u.id, c.id if c else None))


async def edit_msg(q, text, markup):
    try:
        await q.edit_message_text(text, reply_markup=markup)
    except TelegramError:
        pass


# ----------------------------- callbacks -----------------------------
async def restart_engine(app):
    await run.set_engine(app, False)
    await asyncio.sleep(1)
    return await run.set_engine(app, True)


async def on_menu(update, context):
    q, app = update.callback_query, context.application
    if not adm(update, context):
        await q.answer("Admins only.", show_alert=True)
        return
    key, note = q.data.split(":", 1)[1], ""
    if key == "restart":
        note, key = await restart_engine(app), "eng"
    elif key in ("start", "stop"):
        note, key = await run.set_engine(app, key == "start"), "eng"
    await q.answer(note[:190])
    text, markup = await render(app, key)
    await edit_msg(q, text, markup)


async def on_edit(update, context):
    q = update.callback_query
    if not adm(update, context):
        await q.answer("Admins only.", show_alert=True)
        return
    key = q.data.split(":", 1)[1]
    context.user_data["await"] = key
    await q.answer()
    label, getter = SETTINGS[key][0], SETTINGS[key][2]
    await q.message.reply_text(f"✏️ {label}\nCurrent value:\n{getter()}\n\nSend the new value now (or /cancel).",
                               link_preview_options=run.NO_PREVIEW)


async def on_toggle(update, context):
    q, app = update.callback_query, context.application
    if not adm(update, context):
        await q.answer("Admins only.", show_alert=True)
        return
    key = q.data.split(":", 1)[1]
    new = not SETTINGS[key][2]()
    SETTINGS[key][3](new)
    CFG["vals"][key] = new
    await cfg_save(app)
    await q.answer("ON" if new else "OFF")
    text, markup = await render(app, BOOL_PANEL.get(key, "edit"))
    await edit_msg(q, text, markup)


async def on_fwd(update, context):
    q, app = update.callback_query, context.application
    if not adm(update, context):
        await q.answer("Admins only.", show_alert=True)
        return
    act = q.data.split(":", 1)[1]
    if act == "set":
        context.user_data["await"] = "channel"
        await q.answer()
        await q.message.reply_text("🔗 CONNECT CHANNEL\n1) Add this bot to your channel as ADMIN with 'Post messages' permission.\n"
                                   "2) Send the channel @username or numeric ID (like -1001234567890).\n/cancel to stop.")
        return
    if act == "off":
        CFG.update(channel_id=None, channel_title="")
        await cfg_save(app)
        await q.answer("Disconnected.")
    elif act == "test":
        if not CFG["channel_id"]:
            await q.answer("No channel connected.", show_alert=True)
            return
        try:
            await app.bot.send_message(CFG["channel_id"], "🧪 Test message from the signal bot.")
            await q.answer("Test sent ✅", show_alert=True)
        except TelegramError as exc:
            await q.answer(f"Failed: {type(exc).__name__}", show_alert=True)
    text, markup = await render(app, "fwd")
    await edit_msg(q, text, markup)


async def on_promo_now(update, context):
    q = update.callback_query
    if not adm(update, context):
        await q.answer("Admins only.", show_alert=True)
        return
    await q.answer(await send_promo(context.application), show_alert=True)


async def on_text(update, context):
    key = context.user_data.get("await")
    if not key or not adm(update, context):
        await member_text(update, context)
        return
    app, text = context.application, update.effective_message.text
    try:
        if key == "channel":
            name = await connect_channel(app, text)
            reply = f"✅ Channel connected: {name}\nTest message sent. Every confirmed signal will now be posted there."
        else:
            label, _k, _g, setter, validator = SETTINGS[key]
            val = validator(text)
            setter(val)
            CFG["vals"][key] = val
            await cfg_save(app)
            reply = f"✅ Updated: {label}\nNew value: {SETTINGS[key][2]()}"
    except ValueError as exc:
        await update.effective_message.reply_text(f"❌ {exc}\nTry again or /cancel.")
        raise ApplicationHandlerStop
    context.user_data.pop("await", None)
    await update.effective_message.reply_text(reply, link_preview_options=run.NO_PREVIEW)
    raise ApplicationHandlerStop


# ----------------------------- commands (admin only) -----------------------------
def admin_only(fn):
    async def wrapper(update, context):
        if adm(update, context):
            await fn(update, context)
    return wrapper


def panel_cmd(key):
    async def fn(update, context):
        text, markup = await render(context.application, key)
        await update.effective_message.reply_text(text, reply_markup=markup)
    return admin_only(fn)


@admin_only
async def cmd_cancel(update, context):
    context.user_data.pop("await", None)
    await update.effective_message.reply_text("Cancelled.")


@admin_only
async def cmd_restart(update, context):
    await update.effective_message.reply_text(await restart_engine(context.application))


class _Ctx:
    def __init__(self, c, args):
        self.application, self.bot, self.user_data, self.args = c.application, c.bot, c.user_data, args


@admin_only
async def cmd_scan(update, context):
    await run.autosignal_cmd(update, _Ctx(context, ["now"]))


async def ensure_results(pool):
    await pool.execute("CREATE TABLE IF NOT EXISTS master_results (k TEXT PRIMARY KEY, r TEXT NOT NULL)")


async def signals_view(app):
    pool = run._pool(app)
    if pool is None:
        return "Signal history needs PostgreSQL (DATABASE_URL).", None
    try:
        await ensure_results(pool)
        rows = await pool.fetch("SELECT pair, tf, direction, score, created_at FROM bot_signal_log "
                                "ORDER BY created_at DESC LIMIT 10")
        res = {r["k"]: r["r"] for r in await pool.fetch("SELECT k, r FROM master_results")}
    except Exception as exc:  # noqa: BLE001
        return f"History unavailable ({type(exc).__name__}).", None
    lines, buttons = [], []
    for r in rows:
        ep = int(r["created_at"].timestamp())
        key = f"{ep}|{r['pair']}|{r['tf']}"
        label = f"{r['pair']} {run.TF_LABEL[r['tf']]} {r['direction']}"
        lines.append(f"{ {'W': '✅', 'L': '❌'}.get(res.get(key), '⏳') } {ist(r['created_at'])} | {label} {r['score']:+d}")
        if key not in res:
            buttons.append([(f"✅ {label}", f"r:W:{ep}:{r['pair']}:{r['tf']}"), ("❌ Loss", f"r:L:{ep}:{r['pair']}:{r['tf']}")])
    text = ("🕘 RECENT SIGNALS (IST)\n" + "\n".join(lines or ["none yet"]) +
            "\n\n⏳ = result not recorded. After the trade expires, check your Quotex result and tap ✅ (win) or ❌ Loss.")
    return text, (kb(buttons) if buttons else None)


@admin_only
async def cmd_signals(update, context):
    text, markup = await signals_view(context.application)
    await update.effective_message.reply_text(text, reply_markup=markup)


async def on_result(update, context):
    q, app = update.callback_query, context.application
    if not adm(update, context):
        await q.answer("Admins only.", show_alert=True)
        return
    try:
        _, res, ep, pair, tf = q.data.split(":", 4)
        pool = run._pool(app)
        await ensure_results(pool)
        await pool.execute("INSERT INTO master_results (k, r) VALUES ($1, $2) "
                           "ON CONFLICT (k) DO UPDATE SET r = EXCLUDED.r", f"{ep}|{pair}|{tf}", "W" if res == "W" else "L")
    except Exception as exc:  # noqa: BLE001
        await q.answer(f"Could not save ({type(exc).__name__})", show_alert=True)
        return
    await q.answer("Saved ✅")
    text, markup = await signals_view(app)
    await edit_msg(q, text, markup)


@admin_only
async def cmd_stats(update, context):
    await update.effective_message.reply_text(await stats_text(context.application))


# ----------------------------- delete Trader IDs (admin only, with confirmation) -----------------------------
TID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _tids(arg):
    return ["99999", "12345"] if arg.lower() == "tests" else ([arg] if TID_RE.match(arg) else [])


async def _del_counts(pool, ids):
    ev = await pool.fetchval("SELECT count(*) FROM bot_events WHERE trader_id = ANY($1::text[])", ids)
    sub = await pool.fetchval("SELECT count(*) FROM bot_submissions WHERE trader_id = ANY($1::text[])", ids)
    return ev, sub


@admin_only
async def cmd_deltrader(update, context):
    msg = update.effective_message
    pool = run._pool(context.application)
    ids = _tids(context.args[0].strip()) if context.args else []
    if pool is None or not ids:
        await msg.reply_text("Usage:\n/deltrader 96534641  (one Trader ID)\n/deltrader tests  (the fake test IDs 99999 and 12345)")
        return
    ev, sub = await _del_counts(pool, ids)
    await msg.reply_text(
        f"🗑 Delete Trader ID: {', '.join(ids)}\nPostback events: {ev}\nSubmissions/approvals: {sub}\n"
        "This cannot be undone.",
        reply_markup=kb([[("✅ Yes, delete", f"d:y:{context.args[0].strip()[:32]}"), ("❌ Cancel", "d:n:x")]]))


async def on_del(update, context):
    q, app = update.callback_query, context.application
    if not adm(update, context):
        await q.answer("Admins only.", show_alert=True)
        return
    _, act, arg = q.data.split(":", 2)
    if act != "y":
        await q.answer("Cancelled.")
        await edit_msg(q, "Cancelled. Nothing was deleted.", None)
        return
    pool, ids = run._pool(app), _tids(arg)
    if pool is None or not ids:
        await q.answer("Not possible.", show_alert=True)
        return
    try:
        r1 = await pool.execute("DELETE FROM bot_events WHERE trader_id = ANY($1::text[])", ids)
        r2 = await pool.execute("DELETE FROM bot_submissions WHERE trader_id = ANY($1::text[])", ids)
    except Exception as exc:  # noqa: BLE001
        await q.answer(f"Failed: {type(exc).__name__}", show_alert=True)
        return
    await q.answer("Deleted ✅")
    await edit_msg(q, f"✅ Deleted Trader ID: {', '.join(ids)}\nEvents removed: {r1.split()[-1]}\n"
                      f"Submissions removed: {r2.split()[-1]}", None)


# ----------------------------- strict Trader ID check (uses the Quotex postback data) -----------------------------
def min_deposit(settings):
    v = settings.min_deposit_value
    return v if v is not None else Decimal(str(CFG["min_deposit"]))


async def real_summary(store, tid):
    """Postback summary for a Trader ID, ignoring the fake test events (event_id starting with test / chk-)."""
    pool = getattr(store, "_pool", None)
    if pool is None:
        return await store.trader_summary(tid)
    r = await pool.fetchrow(
        """SELECT count(*) AS n, coalesce(bool_or(kind='reg'), false) AS reg, coalesce(bool_or(kind='ftd'), false) AS ftd,
                  coalesce(sum(amount) FILTER (WHERE kind IN ('ftd','dep')), 0) AS dep_total,
                  count(*) FILTER (WHERE kind IN ('ftd','dep')) AS dep_n
           FROM bot_events WHERE trader_id=$1 AND event_id NOT ILIKE 'test%' AND event_id NOT ILIKE 'chk-%'""", tid)
    if not r or r["n"] == 0:
        return None
    return {"registered": r["reg"], "ftd": r["ftd"], "deposits": Decimal(r["dep_total"]),
            "deposit_count": r["dep_n"], "events": r["n"]}


_orig_try = bot_main.try_auto_approve
_orig_submit = bot_main._submit


async def strict_auto_approve(store, settings, trader_id):
    if not CFG["strict"]:
        return await _orig_try(store, settings, trader_id)
    pending = await store.get_by_trader(trader_id)
    if not pending or pending["status"] != "pending":
        return None
    summ = await real_summary(store, trader_id)
    if not (summ and summ["deposit_count"] > 0 and summ["deposits"] >= min_deposit(settings)):
        return None
    return await store.decide(trader_id, "approved", 0)          # decided_by 0 = automatic


CONNECTED = "✅ Successfully your Trader ID is connected"


def _usd(x):
    return "$" + format(Decimal(x).quantize(Decimal("0.01")), "f")


def _usd_min(x):
    return "$" + format(Decimal(x).normalize(), "f")


def menu_kb():
    return KB([[B("▶️ Start Signals", callback_data="sig:start"), B("🔕 Stop Signals", callback_data="sig:stop")]])


async def send_connected(app, uid):
    """Connected message + Start/Stop buttons. Signals begin when the member taps Start Signals."""
    await run.set_optout(app, uid, True)
    vip = app.bot_data["settings"].vip_link
    await app.bot.send_message(uid, CONNECTED + (f"\nVIP access: {vip}" if vip else ""), reply_markup=menu_kb())


async def strict_submit(update, context, raw):
    if not CFG["strict"]:
        return await _orig_submit(update, context, raw)
    bd = context.application.bot_data
    s, store, user, msg = bd["settings"], bd["store"], update.effective_user, update.effective_message
    tid = (raw or "").strip()
    mine = await store.get_by_user(user.id)
    if mine and mine["status"] == "pending":
        return await _orig_submit(update, context, raw)           # an old manual request: original reply
    if mine and mine["status"] == "approved":
        context.user_data.pop(bot_main.AWAITING, None)
        await msg.reply_text(CONNECTED, reply_markup=menu_kb())
        return
    context.user_data[bot_main.AWAITING] = True                   # after a failed try the next message is a new try
    summ = await real_summary(store, tid) if s.trader_id_re.match(tid) else None
    if summ is None:
        await msg.reply_text("❌ NOT FOUND")
        return
    need, have = min_deposit(s), summ["deposits"]
    if not (summ["deposit_count"] > 0 and have >= need):
        await msg.reply_text(f"✅ Trader ID found\n💰 Minimum Deposit: {_usd_min(need)}\n💳 Your Deposit: {_usd(have)}\n"
                             f"➡️ Deposit minimum {_usd_min(need)}, then send your Trader ID again.")
        return
    res = await bot_main.submit_trader_id(store, s, user.id, user.username, tid)
    if res.code == "taken":
        await msg.reply_text("❌ This Trader ID is already linked to another Telegram account.")
        return
    if res.code != "submitted":
        return await _orig_submit(update, context, raw)
    context.user_data.pop(bot_main.AWAITING, None)
    done = await store.decide(tid, "approved", 0)                 # decided_by 0 = automatic
    if done:
        await send_connected(context.application, user.id)
        await bot_main.notify_admins(context.bot, s, f"Auto-approved (verified by postback)\n{bot_main._admin_text(done)}")


async def member_text(update, context):
    """Plain text from a member who is not editing anything: never show the old 'Use /verify ... /help' hint."""
    if not CFG["strict"] or context.user_data.get(bot_main.AWAITING):
        return                                    # strict off: original behaviour / waiting for a Trader ID: original handler
    text = (update.effective_message.text or "").strip()
    if re.fullmatch(r"\d{3,12}", text):
        await strict_submit(update, context, text)   # a bare number is treated as a Trader ID
    raise ApplicationHandlerStop                  # anything else: stay silent


_orig_on_decision = bot_main.on_decision


async def on_decision_wrap(update, context):
    """Admin pressed Approve/Reject: original handler first, then the Start/Stop buttons for the member."""
    await _orig_on_decision(update, context)
    q = update.callback_query
    try:
        if q and (q.data or "").startswith("ok:"):
            row = await context.application.bot_data["store"].get_by_trader(q.data.split(":", 1)[1])
            at = row.get("decided_at") if row else None
            if row and row["status"] == "approved" and at and (datetime.now(timezone.utc) - at).total_seconds() < 20:
                await run.set_optout(context.application, row["telegram_id"], True)
                await context.bot.send_message(row["telegram_id"], "🔔 SIGNALS", reply_markup=menu_kb())
    except Exception as exc:  # noqa: BLE001
        log.warning("approve menu failed: %s", type(exc).__name__)
    raise ApplicationHandlerStop


bot_main.approval_text = lambda s, row: CONNECTED + (f"\nVIP access: {s.vip_link}" if s.vip_link else "")


# ---- every normal bot message in bold ("dark" text). Telegram cannot change font or colour, only bold/italic/mono.
NEW_REF = "https://broker-qx.pro/sign-up/?lid=2432286"
_REF_RE = re.compile(r"https?://[\w.-]*qx[\w.-]*/sign-up/\?lid=\d+")


def _ref(x):
    """Every Quotex sign-up link in any outgoing text becomes the new referral link."""
    return _REF_RE.sub(NEW_REF, x) if isinstance(x, str) else x


def _ref_markup(rm):
    rows = getattr(rm, "inline_keyboard", None)
    if not rows or not any(getattr(b, "url", None) and _REF_RE.search(b.url) for r in rows for b in r):
        return rm
    return KB([[B(b.text, url=_ref(b.url)) if getattr(b, "url", None) else b for b in r] for r in rows])


def _unset(v):
    return v is None or type(v).__name__ == "DefaultValue"


_o_send_message, _o_send_photo = ExtBot.send_message, ExtBot.send_photo


async def _bold_message(self, *a, **k):
    text = k["text"] if "text" in k else (a[1] if len(a) > 1 else None)
    if isinstance(text, str):
        if "text" in k:
            k["text"] = text = _ref(text)
        else:
            text = _ref(text)
            a = (a[0], text) + tuple(a[2:])
    if "reply_markup" in k:
        k["reply_markup"] = _ref_markup(k["reply_markup"])
    if isinstance(text, str) and text and _unset(k.get("parse_mode")) and len(a) < 3 and not k.get("entities"):
        wrapped = "<b>" + html.escape(text) + "</b>"
        if "text" in k:
            k["text"] = wrapped
        else:
            a = (a[0], wrapped) + tuple(a[2:])
        k["parse_mode"] = "HTML"
    return await _o_send_message(self, *a, **k)


async def _bold_photo(self, *a, **k):
    if isinstance(k.get("caption"), str):
        k["caption"] = _ref(k["caption"])
    if "reply_markup" in k:
        k["reply_markup"] = _ref_markup(k["reply_markup"])
    cap = k.get("caption")
    if isinstance(cap, str) and cap and _unset(k.get("parse_mode")) and not k.get("caption_entities"):
        k["caption"], k["parse_mode"] = "<b>" + html.escape(cap) + "</b>", "HTML"
    return await _o_send_photo(self, *a, **k)


ExtBot.send_message, ExtBot.send_photo = _bold_message, _bold_photo


bot_main.try_auto_approve = strict_auto_approve
bot_main._submit = strict_submit


# ----------------------------- /trader : list + approve / reject / unlink / delete -----------------------------
TR_PAGE, TR_ICON = 8, {"approved": "✅", "pending": "⏳", "rejected": "⛔"}


async def tr_list(app, page):
    rows = await run._pool(app).fetch("SELECT trader_id, status, username FROM bot_submissions ORDER BY submitted_at DESC")
    if not rows:
        return "No Trader IDs yet.", None
    pages = (len(rows) + TR_PAGE - 1) // TR_PAGE
    page = max(0, min(page, pages - 1))
    btn = [[(f"{TR_ICON.get(r['status'], '•')} {r['trader_id']}" + (f"  @{r['username']}" if r["username"] else ""),
             f"tr:v:{r['trader_id']}")] for r in rows[page * TR_PAGE:(page + 1) * TR_PAGE]]
    nav = ([("⬅️ Prev", f"tr:l:{page - 1}")] if page > 0 else []) + ([("➡️ Next", f"tr:l:{page + 1}")] if page < pages - 1 else [])
    return (f"👤 TRADER IDs ({len(rows)}) - page {page + 1}/{pages}\n✅ approved  ⏳ pending  ⛔ rejected\nTap one to manage it.",
            kb(btn + ([nav] if nav else [])))


async def tr_detail(app, tid):
    s, store = app.bot_data["settings"], app.bot_data["store"]
    row = await run._pool(app).fetchrow("SELECT * FROM bot_submissions WHERE trader_id=$1", tid)
    req = (f"Telegram request: {row['status']}\nTelegram ID: {row['telegram_id']}"
           + (f"\nUsername: @{row['username']}" if row["username"] else "")) if row else "Telegram request: none"
    rows = []
    if row:
        a = ([("✅ Approve", f"tr:a:{tid}")] if row["status"] != "approved" else []) + \
            ([("⛔ Reject", f"tr:r:{tid}")] if row["status"] != "rejected" else [])
        rows += ([a] if a else []) + [[("🔓 Unlink", f"tr:u:{tid}")]]
    rows += [[("🗑 Delete", f"tr:d:{tid}")], [("⬅️ Back", "tr:l:0")]]
    return f"Trader ID {tid}\n{req}\n{bot_main.describe_summary(s, await real_summary(store, tid))}", kb(rows)


async def cmd_trader(update, context):
    if not adm(update, context):
        return                                    # the original /trader ignores non-admins as well
    app = context.application
    try:
        if run._pool(app) is None:
            await update.effective_message.reply_text("This needs PostgreSQL (DATABASE_URL).")
        else:
            tid = context.args[0].strip()[:32] if context.args else None
            text, markup = await (tr_detail(app, tid) if tid else tr_list(app, 0))
            await update.effective_message.reply_text(text, reply_markup=markup)
    except Exception as exc:  # noqa: BLE001
        log.warning("/trader failed: %s", type(exc).__name__)
        await update.effective_message.reply_text(f"Could not load Trader IDs ({type(exc).__name__}).")
    raise ApplicationHandlerStop


async def on_trader(update, context):
    q, app = update.callback_query, context.application
    if not adm(update, context):
        await q.answer("Admins only.", show_alert=True)
        return
    pool = run._pool(app)
    if pool is None:
        await q.answer("Needs PostgreSQL.", show_alert=True)
        return
    _, act, arg = q.data.split(":", 2)
    s = app.bot_data["settings"]
    try:
        if act == "l":
            text, markup = await tr_list(app, int(arg))
        elif act == "v":
            text, markup = await tr_detail(app, arg)
        elif act in ("a", "r"):
            status = "approved" if act == "a" else "rejected"
            row = await pool.fetchrow("UPDATE bot_submissions SET status=$2, decided_at=now(), decided_by=$3 "
                                      "WHERE trader_id=$1 RETURNING *", arg, status, q.from_user.id)
            if row:
                try:
                    if act == "a":
                        await send_connected(app, row["telegram_id"])
                    else:
                        await app.bot.send_message(row["telegram_id"], f"❌ NOT FOUND")
                except Exception:  # noqa: BLE001
                    pass
            text, markup = await tr_detail(app, arg)
        elif act in ("u", "d"):
            what = ("Unlink: removes the link to the Telegram user (they must verify again). Postback data is kept."
                    if act == "u" else "Delete: removes the Telegram link AND all postback events of this ID.")
            text, markup = f"⚠️ {what}\nTrader ID: {arg}\nThis cannot be undone.", \
                kb([[("✅ Yes", f"tr:{act.upper()}:{arg}"), ("❌ Cancel", f"tr:v:{arg}")]])
        else:                                          # "U" / "D" = confirmed
            n_link = (await pool.execute("DELETE FROM bot_submissions WHERE trader_id=$1", arg)).split()[-1]
            n_ev = (await pool.execute("DELETE FROM bot_events WHERE trader_id=$1", arg)).split()[-1] if act == "D" else None
            text = (f"🗑 Deleted {arg}: {n_link} link, {n_ev} postback events." if n_ev is not None
                    else f"🔓 Unlinked {arg}: {n_link} link removed.")
            markup = kb([[("⬅️ Back to list", "tr:l:0")]])
    except Exception as exc:  # noqa: BLE001
        await q.answer(f"Failed: {type(exc).__name__}", show_alert=True)
        return
    await q.answer()
    await edit_msg(q, text, markup)


# ----------------------------- strong 2-minute analysis (replaces the simple 5-point check) -----------------------------
def _atr(c, n=14):
    trs = [max(x["h"] - x["l"], abs(x["h"] - p["c"]), abs(x["l"] - p["c"])) for p, x in zip(c, c[1:])]
    if len(trs) < n:
        return None
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def _adx(c, n=14):
    if len(c) < 2 * n + 2:
        return None
    tr, pdm, ndm = [], [], []
    for p, x in zip(c, c[1:]):
        up, dn = x["h"] - p["h"], p["l"] - x["l"]
        pdm.append(up if up > dn and up > 0 else 0.0)
        ndm.append(dn if dn > up and dn > 0 else 0.0)
        tr.append(max(x["h"] - x["l"], abs(x["h"] - p["c"]), abs(x["l"] - p["c"])))
    atr, pd, nd, dxs = sum(tr[:n]), sum(pdm[:n]), sum(ndm[:n]), []
    for i in range(n, len(tr)):
        atr, pd, nd = atr - atr / n + tr[i], pd - pd / n + pdm[i], nd - nd / n + ndm[i]
        pdi, ndi = (100 * pd / atr, 100 * nd / atr) if atr else (0.0, 0.0)
        dxs.append((100 * abs(pdi - ndi) / (pdi + ndi) if pdi + ndi else 0.0, pdi, ndi))
    if len(dxs) < n:
        return None
    adx = sum(d[0] for d in dxs[:n]) / n
    for d in dxs[n:]:
        adx = (adx * (n - 1) + d[0]) / n
    return adx, dxs[-1][1], dxs[-1][2]


def _stoch(c, k=14, d=3):
    if len(c) < k + d:
        return None
    ks = []
    for i in range(k - 1, len(c)):
        w = c[i - k + 1:i + 1]
        hh, ll = max(x["h"] for x in w), min(x["l"] for x in w)
        ks.append(100 * (c[i]["c"] - ll) / (hh - ll) if hh > ll else 50.0)
    return ks[-1], sum(ks[-d:]) / d


def _boll(closes, n=20, k=2.0):
    w = closes[-n:]
    m = sum(w) / n
    sd = (sum((x - m) ** 2 for x in w) / n) ** 0.5
    return m, m + k * sd, m - k * sd


def _supertrend(c, n=10, mult=3.0):
    if len(c) < n + 3:
        return None
    trs = [max(x["h"] - x["l"], abs(x["h"] - p["c"]), abs(x["l"] - p["c"])) for p, x in zip(c, c[1:])]
    atr = sum(trs[:n]) / n                     # atr for candle index n
    fub = flb = None
    trend = 1
    for i in range(n, len(c)):
        if i > n:
            atr = (atr * (n - 1) + trs[i - 1]) / n
        hl2 = (c[i]["h"] + c[i]["l"]) / 2
        ub, lb = hl2 + mult * atr, hl2 - mult * atr
        if fub is None:
            fub, flb, trend = ub, lb, (1 if c[i]["c"] > hl2 else -1)
            continue
        fub = ub if (ub < fub or c[i - 1]["c"] > fub) else fub
        flb = lb if (lb > flb or c[i - 1]["c"] < flb) else flb
        if trend == 1 and c[i]["c"] < flb:
            trend = -1
        elif trend == -1 and c[i]["c"] > fub:
            trend = 1
    return trend


def _sr_room(c):
    """Distance from the last close up to the nearest swing high and down to the nearest swing low."""
    last, look, res, sup = c[-1]["c"], c[-90:-2], [], []
    for i in range(2, len(look) - 2):
        w = look[i - 2:i + 3]
        if look[i]["h"] >= max(x["h"] for x in w):
            res.append(look[i]["h"])
        if look[i]["l"] <= min(x["l"] for x in w):
            sup.append(look[i]["l"])
    above, below = [r for r in res if r > last], [x for x in sup if x < last]
    return (min(above) - last if above else None), (last - max(below) if below else None)


def _pattern(c):
    a, b = c[-2], c[-1]
    body = lambda x: abs(x["c"] - x["o"])
    rng = lambda x: (x["h"] - x["l"]) or 1e-12
    up = lambda x: x["c"] > x["o"]
    v = 0.0
    if not up(a) and up(b) and b["c"] >= a["o"] and b["o"] <= a["c"] and body(b) > body(a):
        v += 1.0                                           # bullish engulfing
    if up(a) and not up(b) and b["o"] >= a["c"] and b["c"] <= a["o"] and body(b) > body(a):
        v -= 1.0                                           # bearish engulfing
    lw, uw = min(b["o"], b["c"]) - b["l"], b["h"] - max(b["o"], b["c"])
    if body(b) > 0 and lw >= 2 * body(b) and uw <= body(b):
        v += 0.7                                           # hammer / pin bar
    if body(b) > 0 and uw >= 2 * body(b) and lw <= body(b):
        v -= 0.7                                           # shooting star
    if body(b) / rng(b) >= 0.7:
        v += 0.5 if up(b) else -0.5                        # strong full-body candle
    return max(-1.0, min(1.0, v))


def _resample5(c):
    out, cur, key = [], None, None
    for x in c:
        k = int(x["t"].timestamp() // 300)
        if k != key:
            if cur and cur["n"] == 5:
                out.append(cur)
            cur, key = {"o": x["o"], "h": x["h"], "l": x["l"], "c": x["c"], "n": 1}, k
        else:
            cur["h"], cur["l"], cur["c"] = max(cur["h"], x["h"]), min(cur["l"], x["l"]), x["c"]
            cur["n"] += 1
    if cur and cur["n"] == 5:
        out.append(cur)
    return out


def _clamp(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def strong_evaluate(candles):
    """Weighted vote of 10 independent checks (0-100). Returns the same dict shape as run.evaluate():
    score = +-5 / +-4 when strong enough, 0 when rejected."""
    if len(candles) < 120:
        return None
    closes = [x["c"] for x in candles]
    price = closes[-1]
    atr = _atr(candles)
    e9, e21, e50 = run._ema(closes, 9)[-1], run._ema(closes, 21)[-1], run._ema(closes, 50)[-1]
    r, rp = run._rsi(closes), run._rsi(closes[:-1])
    h, hp = run._macd_hist(closes), run._macd_hist(closes[:-1])
    st, ad, bb, sr = _stoch(candles), _adx(candles), _boll(closes), _sr_room(candles)
    sup = _supertrend(candles)
    c5 = _resample5(candles)
    if None in (atr, r, rp, h, hp, st, ad, sup) or len(c5) < 30 or not price:
        return None
    k5 = [x["c"] for x in c5]
    t5 = run._ema(k5, 9)[-1], run._ema(k5, 21)[-1], k5[-1]
    v5 = 1.0 if (t5[0] > t5[1] and t5[2] > t5[1]) else -1.0 if (t5[0] < t5[1] and t5[2] < t5[1]) else 0.0
    adx, pdi, ndi = ad
    kk, dd = st
    mid, upb, lob = bb
    pb = (price - lob) / (upb - lob) if upb > lob else 0.5
    room = lambda d: 0.5 if d is None else _clamp(d / (1.5 * atr), 0.0, 1.0)
    votes = {                                              # name: (weight, vote -1..+1)
        "ema": (14, _clamp((0.6 if e9 > e21 else -0.6) + (0.2 if price > e21 else -0.2) + (0.2 if e21 > e50 else -0.2))),
        "rsi": (10, _clamp((r - 50) / 15 + (0.2 if r > rp else -0.2))),
        "macd": (12, (0.5 if h > 0 else -0.5) + (0.5 if h > hp else -0.5)),
        "stoch": (8, _clamp((0.7 if kk > dd else -0.7) + (0.3 if kk > 50 else -0.3))),
        "adx": (14, (1.0 if pdi > ndi else -1.0) * min(1.0, adx / 30) if adx >= 18 else 0.0),
        "supertrend": (12, float(sup)),
        "boll": (6, _clamp((pb - 0.5) * 2)),
        "sr": (6, _clamp(room(sr[0]) - room(sr[1]))),
        "candle": (8, _pattern(candles)),
        "tf5": (10, v5),
    }
    net = sum(w * v for w, v in votes.values())
    side = 1 if net >= 0 else -1
    strength = abs(net)
    agree = sum(1 for w, v in votes.values() if v * side > 0.25)
    last, rdist, sdist = candles[-1], sr[0], sr[1]
    body_last = abs(last["c"] - last["o"])
    flat = atr / price < 2e-5
    blocked = (
        flat or adx < 18 or agree < 6 or v5 * side < 0
        or (side == 1 and r > 78) or (side == -1 and r < 22)
        or (side == 1 and rdist is not None and rdist < 0.5 * atr)
        or (side == -1 and sdist is not None and sdist < 0.5 * atr)
        or (body_last > 1.5 * atr and (last["c"] > last["o"]) != (side == 1)))
    ok = (not blocked) and strength >= CFG["min_score"]
    items = []
    if votes["ema"][1] * side > 0:
        items.append("EMA 9 above EMA 21" if side == 1 else "EMA 9 below EMA 21")
    if votes["macd"][1] * side > 0:
        items.append("MACD rising" if side == 1 else "MACD falling")
    items.append(f"RSI {r:.0f}")
    if votes["adx"][1] * side > 0:
        items.append(f"ADX {adx:.0f}")
    if votes["supertrend"][1] * side > 0:
        items.append("Supertrend up" if side == 1 else "Supertrend down")
    if votes["tf5"][1] * side > 0:
        items.append("5M trend up" if side == 1 else "5M trend down")
    return {"score": (side * (5 if strength >= 88 else 4)) if ok else 0, "direction": "CALL" if side == 1 else "PUT",
            "flat": flat, "analysis": "SHORT: " + ", ".join(items), "strength": round(strength)}


async def td_candles(symbol, interval, give_up_at=None):
    """Twelve Data candles (regular forex pairs). 300 candles are still ONE credit; the 5M trend is built from them."""
    await run.LIMITER.acquire(give_up_at)
    resp = await run._http().get("https://api.twelvedata.com/time_series", params={
        "symbol": symbol, "interval": interval, "outputsize": 300, "timezone": "UTC", "apikey": run.TD_KEY})
    data = resp.json()
    if data.get("status") == "error" or "values" not in data:
        code = data.get("code")
        if code == 429:
            daily = "day" in str(data.get("message", "")).lower()
            run.LIMITER.penalize(daily=daily)
            raise run.RateLimited("daily budget" if daily else "per-minute limit")
        raise RuntimeError(f"Twelve Data error code {code}")
    return [{"t": datetime.strptime(v["datetime"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc),
             "o": float(v["open"]), "h": float(v["high"]), "l": float(v["low"]), "c": float(v["close"])}
            for v in reversed(data["values"])]


_o_safe_check = run.safe_check


async def safe_check(app, pair, tf, *a, **k):
    setup, note = await _o_safe_check(app, pair, tf, *a, **k)
    if "error code 4" in note and "error code 429" not in note:      # symbol not available on this plan: stop asking for a day
        app.bot_data.setdefault("closed_until", {})[pair] = run._now() + 86400
    return setup, note


async def rotate_loop(app):
    """More pairs than API requests per minute: start each minute with the next batch so every pair gets scanned."""
    while True:
        await asyncio.sleep(60 - (time.time() % 60) + 20)
        n = min(run.TD_RPM, len(run.PAIRS))
        if len(run.PAIRS) > n:
            run.PAIRS[:] = run.PAIRS[n:] + run.PAIRS[:n]


run.evaluate, run.td_candles, run.safe_check = strong_evaluate, td_candles, safe_check
run.TIMEFRAMES[:] = ["1min"]                       # only 2-minute signals (1M candles)
run.EXPIRY["1min"], run.MIN_SCORE_TF["1min"], run.REQUIRE_ALIGN = 2, 4, False


# ----------------------------- OTC market data (OTCharts) - separate from Twelve Data -----------------------------
# OTC signals are calculated ONLY from OTC candles. Twelve Data keeps serving the regular pairs; the two are never mixed.
try:
    from otcharts import Client as _OtcClient
except Exception:  # noqa: BLE001 - package not installed: OTC stays off, nothing else changes
    _OtcClient = None

OTC_KEY = os.environ.get("OTCHARTS_API_KEY", "").strip()
try:
    OTC_DAILY_REQ = max(1, int(os.environ.get("OTC_DAILY_REQ", "150") or 150))   # REST requests per day the bot may spend
except ValueError:
    OTC_DAILY_REQ = 150
OTC_STREAM = os.environ.get("OTC_STREAM", "1").strip().lower() in {"1", "true", "yes", "on"}
OTC = {"status": "off", "client": None, "symbols": [], "stream_sym": None, "stream": "off", "req_today": 0, "day": None,
       "usage": "not read", "last": "-", "errors": 0}


def otc_label(sym):
    base = (sym[:-4] if sym.lower().endswith("_otc") else sym).upper()
    return (f"{base[:3]}/{base[3:]}" if len(base) == 6 else base) + " (OTC)"


def _to_dt(x):
    if isinstance(x, datetime):
        return x if x.tzinfo else x.replace(tzinfo=timezone.utc)
    if isinstance(x, (int, float)):
        v = float(x)
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, timezone.utc)
    if isinstance(x, str):
        t = x.strip().replace("Z", "+00:00")
        try:
            d = datetime.fromisoformat(t)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            return _to_dt(float(t))
    raise ValueError("unusable time")


def otc_bars(bars):
    """OTCharts bars -> candle dicts (UTC). Invalid / duplicate bars are dropped, nothing is invented."""
    out = {}
    for b in bars:
        try:
            o, h, l, c = (float(getattr(b, k)) for k in ("open", "high", "low", "close"))
            if not all(x == x and 0 < x < 1e12 for x in (o, h, l, c)) or h < max(o, c, l) or l > min(o, c, h):
                continue
            t = _to_dt(b.time)
            out[t] = {"t": t, "o": o, "h": h, "l": l, "c": c}
        except Exception:  # noqa: BLE001
            continue
    return [out[k] for k in sorted(out)]


def otc_spend():
    today = datetime.now(timezone.utc).date()
    if OTC["day"] != today:
        OTC["day"], OTC["req_today"] = today, 0
    if OTC["req_today"] >= OTC_DAILY_REQ:
        return False
    OTC["req_today"] += 1
    return True


class TickCandles:
    """Builds 1-minute candles from live ticks (arrival time, UTC). Minutes without ticks produce no candle."""

    def __init__(self, hist):
        self.hist, self.cur = hist, None

    def add(self, ts, price):
        m, closed = int(ts // 60), None
        if self.cur and self.cur["m"] != m:
            c = self.cur
            t = datetime.fromtimestamp(c["m"] * 60, timezone.utc)
            if not self.hist or t > self.hist[-1]["t"]:
                closed = {"t": t, "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"]}
                self.hist.append(closed)
                del self.hist[:-400]
            self.cur = None
        if self.cur is None:
            self.cur = {"m": m, "o": price, "h": price, "l": price, "c": price}
        else:
            self.cur["h"], self.cur["l"], self.cur["c"] = max(self.cur["h"], price), min(self.cur["l"], price), price
        return closed


async def otc_signal(app, sym, candles):
    """Analyse the newest CLOSED OTC candle and queue a signal (same chart / message / users as the other signals)."""
    pair, d, now = otc_label(sym), app.bot_data, run._now()
    closed = run.closed_only(candles, 60, now)
    if len(closed) < 120:
        return f"only {len(closed)} closed candles"
    t_ts = int(closed[-1]["t"].timestamp())
    if now - (t_ts + 60) > 75:
        return "stale data (market quiet or feed late)"
    seen = d.setdefault("seen", {})
    if seen.get((pair, "1min")) == t_ts:
        return "candle already analysed"
    seen[(pair, "1min")] = t_ts
    res = strong_evaluate(closed)
    if res is None:
        return "not enough data"
    if abs(res["score"]) < run.MIN_SCORE_TF["1min"]:
        return f"no setup (strength {res.get('strength')})"
    if now - d.setdefault("cool", {}).get((pair, "1min"), 0) < run.COOLDOWN_MIN["1min"] * 60:
        return "cooldown"
    await run.dispatch(app, {**res, "pair": pair, "tf": "1min", "t": t_ts, "candles": closed, "aligned": False})
    return f"-> {res['direction']}"


async def otc_rest_loop(app):
    """Round-robin over the OTC pairs, spending at most OTC_DAILY_REQ requests a day (one scan right after a minute closes)."""
    i, cl = 0, OTC["client"]
    while True:
        await asyncio.sleep(60 - (time.time() % 60) + 3)
        if not app.bot_data.get("auto_on"):
            continue
        if int(time.time() // 60) % max(1, round(1440 / OTC_DAILY_REQ)):
            continue
        syms = [x for x in OTC["symbols"] if x != OTC["stream_sym"]] or OTC["symbols"]
        if not syms or not otc_spend():
            continue
        sym = syms[i % len(syms)]
        i += 1
        try:
            bars = await asyncio.to_thread(lambda: cl.candles("quotex", sym, tf=60, limit=300))
            note = await otc_signal(app, sym, otc_bars(bars))
        except Exception as exc:  # noqa: BLE001
            OTC["errors"] += 1
            note = f"error {type(exc).__name__}"
        OTC["last"] = f"{otc_label(sym)}: {note}"


async def otc_stream_loop(app):
    """One live stream (the plan allows one connection): the pair is analysed on every closed minute with no REST requests."""
    cl, sym, loop = OTC["client"], OTC["stream_sym"], asyncio.get_running_loop()
    ticks, stop = asyncio.Queue(maxsize=5000), threading.Event()

    def push(item):
        try:
            ticks.put_nowait(item)
        except asyncio.QueueFull:
            pass

    def reader():                                   # blocking client -> runs in its own thread, reconnects by itself
        while not stop.is_set():
            try:
                for tick in cl.stream("quotex", sym):
                    if stop.is_set():
                        return
                    loop.call_soon_threadsafe(push, (time.time(), float(tick.price)))
            except Exception:  # noqa: BLE001
                OTC["errors"] += 1
            OTC["stream"] = "reconnecting"
            stop.wait(5)

    async def seed():
        if not otc_spend():
            return []
        bars = await asyncio.to_thread(lambda: cl.candles("quotex", sym, tf=60, limit=300))
        return run.closed_only(otc_bars(bars), 60, run._now())

    try:
        try:
            tc = TickCandles(await seed())
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "PlanError":
                OTC["stream"] = "paused: OTCharts plan does not allow data (retry in 30 min)"
                await asyncio.sleep(1800)
            raise
        threading.Thread(target=reader, daemon=True, name="otc-stream").start()
        OTC["stream"] = f"ON ({otc_label(sym)})"
        while True:
            try:
                ts, price = await asyncio.wait_for(ticks.get(), 120)
            except asyncio.TimeoutError:
                OTC["stream"] = "no ticks for 2 min"
                continue
            OTC["stream"] = f"ON ({otc_label(sym)})"
            done = tc.add(ts, price)
            if done and app.bot_data.get("auto_on"):
                if len(tc.hist) >= 2 and (tc.hist[-1]["t"] - tc.hist[-2]["t"]).total_seconds() > 120:
                    tc.hist[:] = await seed() or tc.hist            # gap in the feed: rebuild the history from real candles
                OTC["last"] = f"{otc_label(sym)}: {await otc_signal(app, sym, list(tc.hist))}"
    finally:
        stop.set()


async def otc_boot(app):
    if _OtcClient is None:
        OTC["status"] = "package otcharts not installed (add it to requirements.txt)"
        return
    if not OTC_KEY:
        OTC["status"] = "OTCHARTS_API_KEY not set"
        return
    try:
        cl = _OtcClient()                           # reads OTCHARTS_API_KEY from the environment (documented)
        try:
            syms = await asyncio.to_thread(lambda: [x.symbol for x in cl.symbols("quotex")])
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ != "PlanError":
                raise
            # the plan does not allow listing instruments: use the 5 instruments the README documents for the free plan
            log.warning("OTC symbol list not allowed by this plan (%s): using the 5 documented free instruments",
                        str(exc)[:160].replace(OTC_KEY, "***"))
            syms = ["EURUSD_otc", "GBPUSD_otc", "USDJPY_otc", "BTCUSD_otc", "AUDUSD_otc"]
        if not syms:
            OTC["status"] = "no OTC symbols available for this key"
            return
        try:
            u = await asyncio.to_thread(cl.usage)
            OTC["usage"] = f"{getattr(u, 'remaining', '?')} left of {getattr(u, 'quota_per', '?')}"
        except Exception:  # noqa: BLE001
            OTC["usage"] = "unavailable"
    except Exception as exc:  # noqa: BLE001
        OTC["status"] = f"failed: {type(exc).__name__}"
        log.warning("OTC data start failed: %s: %s", type(exc).__name__, str(exc)[:160].replace(OTC_KEY, "***"))
        return
    pref = os.environ.get("OTC_STREAM_SYMBOL", "").strip()
    OTC.update(client=cl, symbols=syms, status="ON", stream_sym=(pref if pref in syms else syms[0]) if OTC_STREAM else None)
    loop = asyncio.get_running_loop()
    app.bot_data["otc_tasks"] = [loop.create_task(run._forever("otc-rest", otc_rest_loop, app))]
    if OTC["stream_sym"]:
        app.bot_data["otc_tasks"].append(loop.create_task(run._forever("otc-stream", otc_stream_loop, app)))
    log.info("OTC data ready: %d pairs | stream %s | up to %d REST requests/day", len(syms), OTC["stream_sym"] or "off", OTC_DAILY_REQ)


def otc_status_text():
    if OTC["status"] != "ON":
        return f"\n🛰 OTC data: {OTC['status']}"
    return (f"\n🛰 OTC data (OTCharts): ON | {len(OTC['symbols'])} pairs | stream {OTC['stream']} | "
            f"REST today {OTC['req_today']}/{OTC_DAILY_REQ} | quota {OTC['usage']} | errors {OTC['errors']} | last: {OTC['last']}")


_orig_status_all = status_all


async def status_all(app):
    return await _orig_status_all(app) + otc_status_text()


# ----------------------------- hooks into the bot -----------------------------
async def master_boot(app):
    while app.bot_data.get("store") is None or not app.running:
        await asyncio.sleep(2)
    await cfg_load(app)
    apply_saved()
    if CFG.get("migr_ref") != 1:                   # one time: old referral link saved in the database -> new link
        CFG["referral"], CFG["migr_ref"] = NEW_REF, 1
        await cfg_save(app)
    if CFG.get("pairs_v") != 6:                    # one time: Twelve Data is on again -> the 28 core forex pairs
        CFG["vals"]["pairs"], CFG["pairs_v"] = list(ALL_PAIRS), 6
        SETTINGS["pairs"][3](CFG["vals"]["pairs"])
        await cfg_save(app)
    app.bot_data["otc_boot"] = asyncio.get_running_loop().create_task(otc_boot(app))     # OTC data: stream + paced REST
    app.bot_data["rotate_task"] = asyncio.get_running_loop().create_task(run._forever("rotate", rotate_loop, app))
    app.bot_data["master_task"] = asyncio.get_running_loop().create_task(run._forever("promo", promo_loop, app))
    log.info("master upgrade ready | channel %s | promo %s every %sh",
             CFG["channel_id"] or "none", "ON" if CFG["promo_on"] else "OFF", CFG["promo_hours"])


_prev_build = bot_main.build_application
_prev_http = bot_main.make_http_app


def _build(settings, polling):
    app = _prev_build(settings, polling)
    private = filters.ChatType.PRIVATE
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & private, on_text), group=-1)
    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^m:"))
    app.add_handler(CallbackQueryHandler(on_edit, pattern=r"^e:"))
    app.add_handler(CallbackQueryHandler(on_toggle, pattern=r"^t:"))
    app.add_handler(CallbackQueryHandler(on_fwd, pattern=r"^f:"))
    app.add_handler(CallbackQueryHandler(on_promo_now, pattern=r"^p:now$"))
    app.add_handler(CallbackQueryHandler(on_result, pattern=r"^r:"))
    app.add_handler(CallbackQueryHandler(on_del, pattern=r"^d:"))
    app.add_handler(CallbackQueryHandler(on_trader, pattern=r"^tr:"))
    app.add_handler(CallbackQueryHandler(on_decision_wrap, pattern=r"^(ok|no):.{1,60}$"), group=-1)
    app.add_handler(CommandHandler("trader", cmd_trader), group=-1)   # replaces the old one-ID-only /trader for admins
    for name, fn in [("admin", panel_cmd("home")), ("edit", panel_cmd("edit")), ("forwardchannel", panel_cmd("fwd")),
                     ("assets", panel_cmd("assets")), ("cancel", cmd_cancel), ("restartengine", cmd_restart),
                     ("scan", cmd_scan), ("deltrader", cmd_deltrader), ("signals", cmd_signals), ("stats", cmd_stats)]:
        app.add_handler(CommandHandler(name, fn))
    return app


def _http(ctx):
    asyncio.get_running_loop().create_task(master_boot(ctx.ptb))
    return _prev_http(ctx)


bot_main.build_application = _build
bot_main.make_http_app = _http

if __name__ == "__main__":
    bot_main.main()
