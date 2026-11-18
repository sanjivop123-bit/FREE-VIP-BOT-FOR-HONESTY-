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
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as KB, InputFile
from telegram.error import RetryAfter, TelegramError
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
       "referral": "https://broker-qx.pro/sign-up/?lid=2416000",
       "header": "❤️‍🔥 HONESTY TAMIL",
       "disclaimer": "", "vals": {}}


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


def v_pairs(s):
    p = run._pairs(s)
    if not p or len(p) > 20:
        raise ValueError("Send 1-20 pairs like: EUR/USD,GBP/USD,USD/JPY")
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
    "min1": ("Min score 1M (1-5)", "val", lambda: run.MIN_SCORE_TF["1min"], _setkey(run.MIN_SCORE_TF, "1min"), v_int(1, 5)),
    "min5": ("Min score 5M (1-5)", "val", lambda: run.MIN_SCORE_TF["5min"], _setkey(run.MIN_SCORE_TF, "5min"), v_int(1, 5)),
    "cool1": ("Cooldown 1M (min)", "val", lambda: run.COOLDOWN_MIN["1min"], _setkey(run.COOLDOWN_MIN, "1min"), v_int(0, 240)),
    "cool5": ("Cooldown 5M (min)", "val", lambda: run.COOLDOWN_MIN["5min"], _setkey(run.COOLDOWN_MIN, "5min"), v_int(0, 240)),
    "exp1": ("Expiry 1M signals (min)", "val", lambda: run.EXPIRY["1min"], _setkey(run.EXPIRY, "1min"), v_int(1, 30)),
    "exp5": ("Expiry 5M signals (min)", "val", lambda: run.EXPIRY["5min"], _setkey(run.EXPIRY, "5min"), v_int(1, 60)),
    "pairs": ("Enabled pairs", "val", lambda: ",".join(run.PAIRS), _setlist(run.PAIRS), v_pairs),
    "tfs": ("Timeframes", "val", lambda: ",".join(run.TF_LABEL[t] for t in run.TIMEFRAMES), _setlist(run.TIMEFRAMES), v_tfs),
    "offset": ("Fetch delay after candle close (s)", "val", lambda: run.FETCH_OFFSET, _setrun("FETCH_OFFSET"), v_int(0, 30)),
    "align": ("Require 1M+5M trend alignment", "bool", lambda: run.REQUIRE_ALIGN, _setrun("REQUIRE_ALIGN"), None),
    "auto_forward": ("Auto-forward signals to channel", "bool", lambda: CFG["auto_forward"], _setkey(CFG, "auto_forward"), None),
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


def build_caption(pair, tf, res, entry_ts, aligned=False, test=False):
    """Original signal message, only: header = your name, disclaimer + Timeframe lines removed, REGISTER link added at the bottom."""
    text = _orig_caption(pair, tf, res, entry_ts, aligned=aligned, test=test)
    text = text.replace("🔷 HONESTY AI VIP BOT", CFG["header"], 1)
    lines = [ln for ln in text.split("\n")
             if not ln.startswith(("⚠️ Trade responsibly", "⏱ Timeframe"))]
    out = html.escape("\n".join(lines))
    return out + f'\n🔗 <a href="{html.escape(CFG["referral"], quote=True)}">REGISTER ON QUOTEX</a>'


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
        return ("🧠 ANALYSIS SETTINGS\nIndicators used (real candle data): EMA 9/21, RSI 14, MACD, candle structure, "
                "flat-market filter, 1M+5M alignment.\nA signal is sent only when enough confirmations agree "
                "(score out of 5)."), kb(edit_rows(["min1", "min5", "align"]) + [BACK])
    if key == "assets":
        return (f"📊 ASSETS\nPairs ({len(run.PAIRS)}): {', '.join(run.PAIRS)}\n"
                f"Timeframes: {SETTINGS['tfs'][2]()}\nData: Twelve Data (regular forex pairs only)."), \
            kb(edit_rows(["pairs", "tfs"]) + [BACK])
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


# ----------------------------- hooks into the bot -----------------------------
async def master_boot(app):
    while app.bot_data.get("store") is None or not app.running:
        await asyncio.sleep(2)
    await cfg_load(app)
    apply_saved()
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
    for name, fn in [("admin", panel_cmd("home")), ("edit", panel_cmd("edit")), ("forwardchannel", panel_cmd("fwd")),
                     ("assets", panel_cmd("assets")), ("cancel", cmd_cancel), ("restartengine", cmd_restart),
                     ("scan", cmd_scan), ("signals", cmd_signals), ("stats", cmd_stats)]:
        app.add_handler(CommandHandler(name, fn))
    return app


def _http(ctx):
    asyncio.get_running_loop().create_task(master_boot(ctx.ptb))
    return _prev_http(ctx)


bot_main.build_application = _build
bot_main.make_http_app = _http

if __name__ == "__main__":
    bot_main.main()
