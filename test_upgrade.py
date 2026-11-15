"""Tests for the upgraded run.py.   Usage:   python tests/test_upgrade.py  <path-to-ORIGINAL-repo-folder>

Needs only the Python standard library (+ Pillow, optional, to decode the sample charts).
If python-telegram-bot / httpx / tornado are not installed, small stand-ins are used so the real main-4.py and the
new run.py can still be imported and exercised (fake Twelve Data API, fake Telegram bot, controllable clock).
"""
import asyncio
import hashlib
import importlib.util
import inspect
import io
import os
import random
import shutil
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
NEW_DIR = HERE.parent                                   # folder with the upgraded run.py
ORIG_DIR = Path(sys.argv.pop(1) if len(sys.argv) > 1 else os.environ.get("ORIGINAL_DIR", "")).resolve()
SAMPLES = NEW_DIR / "samples"


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


ORIG_HASHES = {p.name: sha(p) for p in ORIG_DIR.iterdir() if p.is_file()}


# --------------------------------------------------------------------------------------------
# stand-ins for libraries that are not installed in the sandbox (never used when the real ones exist)
# --------------------------------------------------------------------------------------------
def install_stubs():
    try:
        import telegram, httpx, tornado.web  # noqa: F401
        return False
    except ImportError:
        pass

    class _F:
        def __and__(self, o): return _F()
        __rand__ = __and__
        def __invert__(self): return _F()

    tg = types.ModuleType("telegram")

    class InlineKeyboardButton:
        def __init__(self, text, callback_data=None, url=None): self.text, self.callback_data = text, callback_data

    class InlineKeyboardMarkup:
        def __init__(self, inline_keyboard): self.inline_keyboard = inline_keyboard

    class Update:
        @staticmethod
        def de_json(d, bot): return d

    class InputFile:
        def __init__(self, obj, filename=None): self.obj, self.filename = obj, filename

    class LinkPreviewOptions:
        def __init__(self, is_disabled=None): self.is_disabled = is_disabled

    tg.InlineKeyboardButton, tg.InlineKeyboardMarkup, tg.Update = InlineKeyboardButton, InlineKeyboardMarkup, Update
    tg.InputFile, tg.LinkPreviewOptions = InputFile, LinkPreviewOptions

    err = types.ModuleType("telegram.error")

    class RetryAfter(Exception):
        def __init__(self, retry_after): self.retry_after = retry_after

    err.RetryAfter = RetryAfter

    ext = types.ModuleType("telegram.ext")

    class _Handler:
        def __init__(self, *a, **k): self.a, self.k = a, k

    class CommandHandler(_Handler):
        def __init__(self, command, callback, filters=None):
            self.commands = [command] if isinstance(command, str) else list(command)
            self.callback = callback

    class CallbackQueryHandler(_Handler):
        def __init__(self, callback, pattern=None): self.callback, self.pattern = callback, pattern

    class MessageHandler(_Handler):
        def __init__(self, filters, callback): self.callback = callback

    class _FakePTB:
        def __init__(self): self.bot_data, self.handlers, self.running, self.bot = {}, [], True, None
        def add_handler(self, h, group=0): self.handlers.append(h)
        def add_error_handler(self, f): pass

    class _Builder:
        def token(self, t): return self
        def updater(self, u): return self
        def build(self): return _FakePTB()

    class Application:
        @staticmethod
        def builder(): return _Builder()

    ext.Application, ext.CommandHandler, ext.CallbackQueryHandler = Application, CommandHandler, CallbackQueryHandler
    ext.MessageHandler, ext.ContextTypes = MessageHandler, SimpleNamespace(DEFAULT_TYPE=object)
    ext.filters = SimpleNamespace(ChatType=SimpleNamespace(PRIVATE=_F()), TEXT=_F(), COMMAND=_F())

    hx = types.ModuleType("httpx")
    hx.AsyncClient = type("AsyncClient", (), {"__init__": lambda self, *a, **k: None})
    td = types.ModuleType("tornado")
    tw = types.ModuleType("tornado.web")
    tw.RequestHandler = type("RequestHandler", (), {})
    tw.Application = type("Application", (), {"__init__": lambda self, routes: None})
    td.web = tw
    for name, mod in (("telegram", tg), ("telegram.error", err), ("telegram.ext", ext), ("httpx", hx),
                      ("tornado", td), ("tornado.web", tw)):
        sys.modules[name] = mod
    return True


STUBBED = install_stubs()


def load(folder, run_file, modname):
    spec = importlib.util.spec_from_file_location(modname, Path(folder) / run_file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


# two isolated working folders (the originals are only READ here)
WORK = Path(tempfile.mkdtemp(prefix="vipbot_test_"))
for sub, run_src in (("new", NEW_DIR / "run.py"), ("old", ORIG_DIR / "run.py")):
    (WORK / sub).mkdir()
    shutil.copy(run_src, WORK / sub / "run.py")
    shutil.copy(ORIG_DIR / "main-4.py", WORK / sub / "main-4.py")
    for img in ORIG_DIR.glob("*.png"):
        shutil.copy(img, WORK / sub / img.name)
run = load(WORK / "new", "run.py", "run_new")
old = load(WORK / "old", "run.py", "run_old")


# --------------------------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------------------------
def make_candles(end_open_ts, step, seed, drift=0.0, vol=0.0003, base=1.10, n=80):
    rnd, price, out = random.Random(seed), base, []
    for i in range(n):
        t = end_open_ts - (n - 1 - i) * step
        o = price
        c = o + drift + rnd.gauss(0, vol)
        h, lo = max(o, c) + abs(rnd.gauss(0, vol * .3)), min(o, c) - abs(rnd.gauss(0, vol * .3))
        out.append({"t": datetime.fromtimestamp(t, timezone.utc), "o": o, "h": h, "l": lo, "c": c})
        price = c
    return out


_SEEDS = {}


def find(direction, base=1.10, vol=0.0003):
    """Deterministic search for candles that the real evaluate() scores strongly CALL / PUT / weakly."""
    key = (direction, base, vol)
    if key not in _SEEDS:
        for seed in range(5000):
            cs = make_candles(1_700_000_000, 60, seed, drift=0.00006 * (1 if direction == "CALL" else -1 if direction == "PUT" else 0),
                              vol=vol, base=base)
            r = run.evaluate(cs)
            if r is None or r["flat"]:
                continue
            if (direction == "CALL" and r["score"] >= 4) or (direction == "PUT" and r["score"] <= -4) \
                    or (direction == "WEAK" and abs(r["score"]) <= 2):
                _SEEDS[key] = seed
                break
        else:
            raise AssertionError(f"no seed for {key}")
    return _SEEDS[key]


class FakeResp:
    def __init__(self, data): self._d = data
    def json(self): return self._d


class FakeClient:
    """Pretends to be Twelve Data. scenario(pair, interval, now) -> list of candles | ('error', code, msg)."""
    def __init__(self, scenario, clock): self.scenario, self.clock, self.calls = scenario, clock, []

    async def get(self, url, params=None):
        pair, tf = params["symbol"], params["interval"]
        self.calls.append((pair, tf))
        now, step = self.clock["t"], {"1min": 60, "5min": 300}[tf]
        res = self.scenario(pair, tf, now)
        if isinstance(res, tuple):
            return FakeResp({"status": "error", "code": res[1], "message": res[2]})
        vals = [{"datetime": c["t"].strftime("%Y-%m-%d %H:%M:%S"), "open": str(c["o"]), "high": str(c["h"]),
                 "low": str(c["l"]), "close": str(c["c"])} for c in reversed(res)]
        return FakeResp({"status": "ok", "values": vals})


def closed_series(now, tf, seed, drift, vol=0.0003, base=1.10, forming=True, age_steps=0):
    step = {"1min": 60, "5min": 300}[tf]
    boundary = int(now // step) * step
    cs = make_candles(boundary - step - age_steps * step, step, seed, drift, vol, base)
    if forming:   # the real API also returns the candle that is still forming; the engine must ignore it
        last = cs[-1]["c"]
        cs.append({"t": datetime.fromtimestamp(boundary, timezone.utc), "o": last, "h": last + .0001, "l": last - .0001, "c": last + .00005})
    return cs


class FakeBot:
    def __init__(self): self.photos, self.messages = [], []

    async def send_photo(self, uid, photo, caption=None, reply_markup=None):
        self.photos.append((uid, photo, caption, reply_markup))
        return SimpleNamespace(photo=[SimpleNamespace(file_id="FILE123")])

    async def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text))


class FakeStore:
    def __init__(self, approved): self.approved = approved
    async def list_approved_ids(self): return list(self.approved)
    async def get_by_user(self, uid): return {"status": "approved"} if uid in self.approved else None


class FakePool:
    """Minimal asyncpg-pool look-alike for the three tables run.py uses."""
    def __init__(self): self.log, self.state, self.optout = {}, {}, set()

    async def execute(self, sql, *a):
        if "INSERT INTO bot_engine_state" in sql:
            self.state["engine_on"] = a[0]

    async def fetchrow(self, sql, *a):
        if "INSERT INTO bot_signal_log" in sql:
            if a[0] in self.log:
                return None
            self.log[a[0]] = {"pair": a[1], "tf": a[2], "ts": datetime.now(timezone.utc)}
            return {"sig_key": a[0]}
        if "FROM bot_engine_state" in sql:
            return {"v": self.state["engine_on"]} if "engine_on" in self.state else None

    async def fetch(self, sql, *a):
        if "FROM bot_signal_log" in sql:
            return [{"pair": v["pair"], "tf": v["tf"], "ts": v["ts"]} for v in self.log.values()]
        return []


def make_app(approved=(10, 11, 12), pool=None, admins=(1,)):
    store = FakeStore(list(approved))
    if pool is not None:
        store._pool = pool
    settings = SimpleNamespace(admin_ids=frozenset(admins), webhook_base_url="",
                               is_admin=lambda uid, chat=None: uid in admins or (chat in admins if chat else False))
    return SimpleNamespace(bot=FakeBot(), running=True, bot_data={"store": store, "settings": settings})


def fake_update(uid=1, args=(), data=None):
    sent = []

    async def reply_text(text, reply_markup=None, **k): sent.append((text, reply_markup))
    msg = SimpleNamespace(reply_text=reply_text, chat_id=uid, chat=SimpleNamespace(id=uid))
    answers, edits = [], []

    async def answer(text=None, show_alert=False): answers.append((text, show_alert))

    async def edit_message_text(text, reply_markup=None): edits.append(text)
    q = SimpleNamespace(data=data, from_user=SimpleNamespace(id=uid), message=msg, answer=answer,
                        edit_message_text=edit_message_text)
    u = SimpleNamespace(effective_user=SimpleNamespace(id=uid, first_name="T"), effective_chat=SimpleNamespace(id=uid),
                        effective_message=msg, callback_query=q)
    return u, SimpleNamespace(args=list(args)), sent, answers, edits


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = {"t": 1_800_000_000 - (1_800_000_000 % 300) + 5}      # 5 s after a 5-minute boundary
        self._saved = {k: getattr(run, k) for k in ("_now", "TD_KEY", "LIMITER", "_http", "PAIRS", "TIMEFRAMES",
                                                     "REQUIRE_ALIGN", "RETRY_WAIT", "TD_RPM", "TD_DAILY_LIMIT")}
        run._now = lambda: self.clock["t"]
        run.TD_KEY = "testkey"
        run.LIMITER = run.RateLimiter(1000, 0)
        run.RETRY_WAIT = 0.01
        self.scenario = lambda pair, tf, now: closed_series(now, tf, 1, 0.0)
        self.client = FakeClient(lambda p, t, n: self.scenario(p, t, n), self.clock)
        run._http = lambda: self.client

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(run, k, v)


# --------------------------------------------------------------------------------------------
class TestPreservation(unittest.TestCase):
    def test_texts_and_buttons_identical(self):
        for n in ("WELCOME_TEXT", "JOIN_TEXT", "NEW_ACCOUNT_TEXT", "GROUP_TEXT", "HELP_TEXT", "DELETE_TEXT", "APPROVED_NOTE", "LINE"):
            self.assertEqual(getattr(run, n), getattr(old, n), n)
        self.assertEqual(run.PAGES, old.PAGES)
        kb = lambda m: [[(b.text, b.callback_data) for b in row] for row in m.inline_keyboard]
        for n in ("WELCOME_KB", "SUBMIT_KB", "STOP_KB", "START_KB"):
            self.assertEqual(kb(getattr(run, n)), kb(getattr(old, n)), n)

    def test_unchanged_functions_have_identical_source(self):
        for n in ("welcome", "on_welcome_button", "_is_member", "_set_pref", "stop_cmd", "start_signals_cmd", "on_sig_button",
                  "_pair_arg", "_find_photo", "evaluate", "_ema", "_rsi", "_macd_hist", "optouts", "set_optout",
                  "send_signal", "_spawn", "_make_http", "_int", "_pairs"):
            self.assertEqual(inspect.getsource(getattr(run, n)), inspect.getsource(getattr(old, n)), n)

    def test_indicator_results_identical(self):
        for d in ("CALL", "PUT", "WEAK"):
            cs = make_candles(1_700_000_000, 60, find(d), 0.00006 if d == "CALL" else -0.00006 if d == "PUT" else 0)
            self.assertEqual(run.evaluate(cs), old.evaluate(cs))

    def test_main4_untouched_and_hooks_installed(self):
        self.assertEqual(sha(WORK / "new" / "main-4.py"), ORIG_HASHES["main-4.py"])
        bm = run.bot_main
        self.assertIs(bm.start, run.welcome_entry)
        self.assertIs(bm.build_application, run._build)
        self.assertIs(bm.make_http_app, run._make_http)
        row = {"trader_id": "T123"}
        s = SimpleNamespace(vip_link="", min_deposit="", currency="USD")
        self.assertTrue(bm.approval_text(s, row).endswith(run.APPROVED_NOTE))

    def test_all_old_and_new_handlers_registered(self):
        settings = SimpleNamespace(token="x")
        app = run._build(settings, True)
        cmds = {c for h in app.handlers for c in getattr(h, "commands", [])}
        for c in ("start", "help", "verify", "status", "pending", "stats", "events", "signal", "trader", "analysis",
                  "stopsignals", "startsignals", "autosignal", "engine", "startengine", "stopengine"):
            self.assertIn(c, cmds, c)
        pats = {h.pattern for h in app.handlers if getattr(h, "pattern", None)}
        for p in (r"^w:(join|new|group|help|old)$", r"^sig:(stop|start)$", r"^eng:(start|stop|status)$",
                  r"^sg:(send|cancel)$", r"^submit$"):
            self.assertIn(p, pats, p)


class TestOriginalsUntouched(unittest.TestCase):
    def test_hashes(self):
        now = {p.name: sha(p) for p in ORIG_DIR.iterdir() if p.is_file()}
        self.assertEqual(now, ORIG_HASHES)


# --------------------------------------------------------------------------------------------
class TestChartAndCaption(Base):
    def test_font_table(self):
        for ch, rows in run.GLYPHS.items():
            self.assertEqual(len(rows), 7, ch)
            self.assertTrue(all(0 <= r < 32 for r in rows), ch)
        for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789./: -+":
            self.assertIn(ch, run.GLYPHS)

    def test_chart_png_valid_with_labels(self):
        from PIL import Image
        SAMPLES.mkdir(exist_ok=True)
        for pair, tf, d, base in (("EUR/USD", "1min", "CALL", 1.10), ("USD/JPY", "5min", "PUT", 150.0)):
            vol = 0.0003 if base < 10 else 0.03
            cs = make_candles(1_800_000_000, run.TF_SECONDS[tf], find(d, base, vol) if d else 3,
                              0.00006 if d == "CALL" else -0.00006, vol, base)
            if base > 10:
                cs = make_candles(1_800_000_000, 300, 7, -0.004, 0.03, base)
            png = run.render_chart(cs, d, f"{pair} {run.TF_LABEL[tf]}  IST", 330)
            im = Image.open(io.BytesIO(png)); im.verify()
            im = Image.open(io.BytesIO(png)).convert("RGB")
            self.assertEqual(im.size, (900, 500))
            px = im.load()
            colours = {px[x, y] for x in range(0, 900, 3) for y in range(0, 500, 3)}
            self.assertIn(run.UP, colours); self.assertIn(run.DN, colours)
            self.assertTrue(any(px[x, 20] == run.TXT for x in range(900)), "title text drawn")
            self.assertTrue(any(px[x, yy] == run.DIM for x in range(900) for yy in range(478, 492)), "time labels drawn")
            (SAMPLES / f"sample_{pair.replace('/', '')}_{run.TF_LABEL[tf]}_{d}.png").write_bytes(png)

    def test_chart_handles_few_candles_and_flat_prices(self):
        flat = [{"t": datetime.fromtimestamp(1_800_000_000 + i * 60, timezone.utc), "o": 1.1, "h": 1.1, "l": 1.1, "c": 1.1} for i in range(5)]
        self.assertTrue(run.render_chart(flat, "PUT", "EUR/USD 1M").startswith(b"\x89PNG"))

    def test_caption(self):
        res = {"direction": "CALL", "score": 4, "analysis": "Short-term momentum looks bullish: x."}
        entry = run.entry_time(self.clock["t"])
        self.assertEqual(entry % 60, 0)
        self.assertGreaterEqual(entry, self.clock["t"] + run.LEAD_SEC)
        self.assertLess(entry, self.clock["t"] + run.LEAD_SEC + 60)
        c1 = run.build_caption("EUR/USD", "1min", res, entry)
        c5 = run.build_caption("EUR/USD", "5min", res, entry, aligned=True, test=True)
        self.assertIn("Timeframe: 1M", c1); self.assertIn("Expiry: 2 Minutes", c1); self.assertIn("🟢 CALL", c1)
        self.assertIn("Timeframe: 5M", c5); self.assertIn("Expiry: 5 Minutes", c5)
        self.assertIn("1M + 5M trend aligned", c5); self.assertIn("TEST SIGNAL", c5)
        self.assertNotIn("aligned", c1)
        self.assertLess(len(c5), 1024)                                      # Telegram caption limit
        run_exp = run.EXPIRY["1min"]; run.EXPIRY["1min"] = 1
        try:
            self.assertIn("Expiry: 1 Minute\n", run.build_caption("EUR/USD", "1min", res, entry))
        finally:
            run.EXPIRY["1min"] = run_exp


class TestTimingHelpers(unittest.TestCase):
    def test_due_boundary(self):
        self.assertIsNone(run.due_boundary(1200 + 2, "1min", None))          # candle closed 2 s ago: wait for offset
        self.assertEqual(run.due_boundary(1200 + 5, "1min", None), 1200)
        self.assertIsNone(run.due_boundary(1200 + 9, "1min", 1200))          # already scheduled
        self.assertEqual(run.due_boundary(1260 + 5, "1min", 1200), 1260)
        self.assertEqual(run.due_boundary(1500 + 5, "5min", None), 1500)
        self.assertIsNone(run.due_boundary(1500 + 150, "5min", 1500))

    def test_closed_only_drops_forming(self):
        now = 1_800_000_065
        cs = closed_series(now, "1min", 1, 0.0)
        self.assertEqual(len(cs), 81)
        closed = run.closed_only(cs, 60, now)
        self.assertEqual(len(closed), 80)
        self.assertEqual(closed[-1]["t"].timestamp(), (now // 60) * 60 - 60)

    def test_timeframe_and_pair_parsing(self):
        self.assertEqual(run._timeframes("1m, 5MIN ,15min,bad,5m"), ["1min", "5min"])
        self.assertEqual(run._tf_arg("5m"), "5min"); self.assertIsNone(run._tf_arg("15m"))


# --------------------------------------------------------------------------------------------
class TestRateLimiter(unittest.IsolatedAsyncioTestCase):
    async def test_never_exceeds_rpm_in_window(self):
        lim, stamps = run.RateLimiter(3, 0, window=0.5), []
        for _ in range(8):
            await lim.acquire(); stamps.append(time.monotonic())
        for i in range(3, len(stamps)):
            self.assertGreaterEqual(stamps[i] - stamps[i - 3], 0.5 - 0.02)
        self.assertGreaterEqual(stamps[-1] - stamps[0], 1.0 - 0.05)

    async def test_daily_budget(self):
        lim = run.RateLimiter(100, 5)
        for _ in range(5): await lim.acquire()
        with self.assertRaises(run.DailyLimit): await lim.acquire()
        self.assertEqual(lim.used_today(), 5)

    async def test_too_late_spends_nothing(self):
        lim = run.RateLimiter(1, 0, window=5.0)
        await lim.acquire()
        with self.assertRaises(run.TooLate): await lim.acquire(give_up_at=time.time() + 0.3)
        self.assertEqual(lim.used_today(), 1)

    async def test_penalize(self):
        lim = run.RateLimiter(10, 0)
        lim.penalize(daily=False); self.assertGreater(lim.blocked_until, time.monotonic() + 4)
        lim2 = run.RateLimiter(10, 0); lim2.penalize(daily=True)
        with self.assertRaises(run.DailyLimit): await lim2.acquire()


class TestTwelveDataErrors(Base):
    async def test_429_minute_daily_and_other_codes(self):
        self.scenario = lambda p, t, n: ("error", 429, "You have run out of API credits for the current minute.")
        with self.assertRaises(run.RateLimited): await run.td_candles("EUR/USD", "1min")
        self.assertGreater(run.LIMITER.blocked_until, time.monotonic())
        run.LIMITER = run.RateLimiter(1000, 0)
        self.scenario = lambda p, t, n: ("error", 429, "You have run out of API credits for the day.")
        with self.assertRaises(run.RateLimited): await run.td_candles("EUR/USD", "1min")
        with self.assertRaises(run.DailyLimit): await run.td_candles("EUR/USD", "1min")
        run.LIMITER = run.RateLimiter(1000, 0)
        self.scenario = lambda p, t, n: ("error", 400, "bad symbol testkey")
        with self.assertRaises(RuntimeError) as cm: await run.td_candles("XXX/YYY", "1min")
        self.assertIn("400", str(cm.exception)); self.assertNotIn("testkey", str(cm.exception))


# --------------------------------------------------------------------------------------------
class TestCheck(Base):
    async def asyncSetUp(self):
        self.app = make_app()
        self.app.bot_data.update(jobq=asyncio.PriorityQueue(), sendq=asyncio.Queue())
        self.call, self.put, self.weak = find("CALL"), find("PUT"), find("WEAK")
        self.boundary = lambda tf: int(self.clock["t"] // run.TF_SECONDS[tf]) * run.TF_SECONDS[tf]

    def up(self, tf="1min"): return lambda p, t, n: closed_series(n, t, self.call, 0.00006)

    async def test_strong_signal_uses_closed_candles_only(self):
        self.scenario = self.up()
        setup, note = await run.check(self.app, "EUR/USD", "1min", self.boundary("1min") - 60)
        self.assertIsNotNone(setup, note)
        self.assertEqual(setup["direction"], "CALL"); self.assertGreaterEqual(abs(setup["score"]), 4)
        self.assertEqual(setup["t"], self.boundary("1min") - 60)                # forming candle was dropped
        self.assertEqual(setup["candles"][-1]["t"].timestamp(), setup["t"])

    async def test_same_candle_only_analysed_once(self):
        self.scenario = self.up()
        a, _ = await run.check(self.app, "EUR/USD", "1min")
        b, note = await run.check(self.app, "EUR/USD", "1min")
        self.assertIsNotNone(a); self.assertIsNone(b); self.assertIn("already analysed", note)

    async def test_cooldown_blocks_next_candle(self):
        self.scenario = self.up()
        s, _ = await run.check(self.app, "EUR/USD", "1min")
        await run.dispatch(self.app, s)
        self.clock["t"] += 60                                                   # next 1m candle, same strong trend
        s2, note = await run.check(self.app, "EUR/USD", "1min")
        self.assertIsNone(s2); self.assertIn("cooldown", note)
        self.clock["t"] += 5 * 60                                               # cooldown (5 min) is over
        s3, note = await run.check(self.app, "EUR/USD", "1min")
        self.assertIsNotNone(s3, note)

    async def test_stale_candle_dropped(self):
        series = closed_series(self.clock["t"], "1min", self.call, 0.00006)      # candles as of the original close
        self.scenario = lambda p, t, n: series
        self.clock["t"] += 100                                                  # data arrives 100 s after the close
        s, note = await run.check(self.app, "EUR/USD", "1min")
        self.assertIsNone(s); self.assertIn("stale", note)

    async def test_market_closed_pauses_pair(self):
        self.scenario = lambda p, t, n: closed_series(n, t, self.call, 0.00006, forming=False, age_steps=200)  # ~3.3 h old
        s, note = await run.check(self.app, "EUR/USD", "1min")
        self.assertIsNone(s); self.assertIn("market closed", note)
        self.assertGreater(self.app.bot_data["closed_until"]["EUR/USD"], self.clock["t"])

    async def test_provider_lag_one_retry(self):
        full = closed_series(self.clock["t"], "1min", self.call, 0.00006)
        calls = []

        def sc(p, t, n):
            calls.append(1)
            return full[:-2] if len(calls) == 1 else full                       # first answer misses the newest closed candle
        self.scenario = sc
        s, note = await run.check(self.app, "EUR/USD", "1min", expected_open=self.boundary("1min") - 60)
        self.assertEqual(len(calls), 2); self.assertIsNotNone(s, note)

    async def test_flat_and_weak_markets_send_nothing(self):
        self.scenario = lambda p, t, n: closed_series(n, t, 1, 0.0, vol=0.0)
        s, note = await run.check(self.app, "EUR/USD", "1min"); self.assertIsNone(s); self.assertIn("flat", note)
        self.scenario = lambda p, t, n: closed_series(n, t, self.weak, 0.0)
        s, note = await run.check(self.app, "GBP/USD", "1min"); self.assertIsNone(s); self.assertIn("score", note)

    async def test_5m_alignment_label_and_requirement(self):
        self.scenario = self.up()
        s5, _ = await run.check(self.app, "EUR/USD", "5min"); self.assertIsNotNone(s5)
        s1, _ = await run.check(self.app, "EUR/USD", "1min"); self.assertTrue(s1["aligned"])
        run.REQUIRE_ALIGN = True
        app2 = make_app(); app2.bot_data.update(jobq=asyncio.PriorityQueue(), sendq=asyncio.Queue())
        self.scenario = lambda p, t, n: closed_series(n, t, self.put, -0.00006) if t == "5min" else closed_series(n, t, self.call, 0.00006)
        await run.check(app2, "EUR/USD", "5min")
        s, note = await run.check(app2, "EUR/USD", "1min")
        self.assertIsNone(s); self.assertIn("not aligned", note)

    async def test_dispatch_blocks_duplicates_and_respects_round_cap(self):
        self.scenario = self.up()
        s, _ = await run.check(self.app, "EUR/USD", "1min")
        self.assertTrue(await run.dispatch(self.app, s))
        self.assertFalse(await run.dispatch(self.app, s))                       # same pair+tf+candle
        self.assertEqual(self.app.bot_data["sendq"].qsize(), 1)
        run.MAX_PER_ROUND = 1
        try:
            s2, _ = await run.check(self.app, "GBP/USD", "1min")
            self.assertFalse(await run.dispatch(self.app, s2))                  # round cap reached
        finally:
            run.MAX_PER_ROUND = 0


class TestPostgresPaths(Base):
    async def test_claim_is_global_across_instances_and_cooldown_restored(self):
        pool = FakePool()
        a, b = make_app(pool=pool), make_app(pool=pool)                         # two Render instances, same database
        self.assertTrue(await run.claim_signal(a, "EUR/USD|1min|100", "EUR/USD", "1min", "CALL", 4))
        self.assertFalse(await run.claim_signal(b, "EUR/USD|1min|100", "EUR/USD", "1min", "CALL", 4))
        self.assertFalse(await run.claim_signal(a, "EUR/USD|1min|100", "EUR/USD", "1min", "CALL", 4))
        c = make_app(pool=pool)
        await run.load_cooldowns(c)
        self.assertIn(("EUR/USD", "1min"), c.bot_data["cool"])

    async def test_engine_state_persists(self):
        pool = FakePool(); app = make_app(pool=pool)
        self.assertIsNone(await run.load_engine_state(app))
        await run.save_engine_state(app, False); self.assertIs(await run.load_engine_state(app), False)
        await run.save_engine_state(app, True); self.assertIs(await run.load_engine_state(app), True)


# --------------------------------------------------------------------------------------------
class TestEngineEndToEnd(Base):
    """Real scheduler + worker + sender loops, fake API, fixed clock: 6 pairs x (1m + 5m)."""

    async def test_full_pipeline_start_stop_and_duplicates(self):
        call, put, weak = find("CALL"), find("PUT"), find("WEAK")

        def sc(pair, tf, now):
            if pair == "EUR/USD": return closed_series(now, tf, call, 0.00006)
            if pair == "GBP/USD": return closed_series(now, tf, put, -0.00006)
            if pair == "USD/JPY": return closed_series(now, tf, 1, 0.0, vol=0.0, base=150.0)       # flat
            if pair == "AUD/USD": return closed_series(now, tf, call, 0.00006) if tf == "5min" else closed_series(now, tf, weak, 0.0)
            if pair == "USD/CAD": return ("error", 400, "bad")                                       # provider error
            return closed_series(now, tf, call, 0.00006, forming=False, age_steps=300)               # EUR/GBP market closed
        self.scenario = sc

        app = make_app(approved=(10, 11, 12), admins=(1,))
        app.bot_data["optout"] = {11}                                           # user 11 pressed "Stop signals"
        await run.signal_engine(app)
        d = app.bot_data
        try:
            self.assertTrue(d["engine_started"]); self.assertTrue(d["auto_on"])
            for _ in range(100):                                               # wait for scheduler -> worker -> sender
                await asyncio.sleep(0.1)
                if len(app.bot.photos) >= 15 and d["jobq"].empty() and d["sendq"].empty():
                    break
            await asyncio.sleep(0.3)
            photos = app.bot.photos
            captions = {c.split("Asset: ")[1].split("\n")[0] + " " + c.split("Timeframe: ")[1].split("\n")[0] for _, _, c, _ in photos}
            self.assertEqual(captions, {"EUR/USD 1M", "EUR/USD 5M", "GBP/USD 1M", "GBP/USD 5M", "AUD/USD 5M"})
            self.assertEqual(len(photos), 5 * 3)                                # 5 signals x (10, 12 + admin 1); user 11 excluded
            self.assertEqual({u for u, *_ in photos}, {10, 12, 1})
            self.assertTrue(all(isinstance(p, run.InputFile) for u, p, c, k in photos[:1]))           # first upload = real PNG
            self.assertTrue(all(p == "FILE123" for u, p, c, k in photos[1:3]))                         # then Telegram file_id reuse
            self.assertTrue(all(k is run.STOP_KB for *_, k in photos))
            by = {c.split("Asset: ")[1].split("\n")[0] + " " + c.split("Timeframe: ")[1].split("\n")[0]: c for _, _, c, _ in photos}
            self.assertIn("🟢 CALL", by["EUR/USD 1M"]); self.assertIn("🔴 PUT", by["GBP/USD 1M"])
            self.assertIn("1M + 5M trend aligned", by["EUR/USD 1M"]); self.assertIn("Expiry: 5 Minutes", by["EUR/USD 5M"])
            self.assertIn("EUR/GBP", d["closed_until"])                         # market-closed pair parked
            calls_first_round = len(self.client.calls)
            self.assertEqual(calls_first_round, 12)                             # exactly one request per pair per timeframe

            # same candle queued again (e.g. restart / double schedule) -> nothing new is sent
            before = len(photos)
            d["jobq"].put_nowait((-60, int(self.clock["t"] // 60) * 60, 999, "EUR/USD", "1min"))
            await asyncio.sleep(0.5)
            self.assertEqual(len(app.bot.photos), before)

            # STOP ENGINE: a new candle closes but nothing is scanned or sent
            self.assertIn("STOPPED", await run.set_engine(app, False))
            self.clock["t"] += 360                                              # +6 min: new 1m and 5m candles
            await asyncio.sleep(1.6)
            self.assertEqual(len(app.bot.photos), before); self.assertEqual(len(self.client.calls), calls_first_round)

            # START ENGINE: scanning resumes on the next closed candle
            self.assertIn("STARTED", await run.set_engine(app, True))
            await asyncio.sleep(1.8)
            self.assertGreater(len(self.client.calls), calls_first_round)
            for _ in range(60):
                await asyncio.sleep(0.1)
                if d["jobq"].empty() and d["sendq"].empty(): break
            self.assertGreater(len(app.bot.photos), before)                     # cooldowns (5 / 15 min) elapsed -> new signals
        finally:
            for t in d.get("engine_tasks", []): t.cancel()
            await asyncio.gather(*d.get("engine_tasks", []), return_exceptions=True)


class TestEngineControls(Base):
    async def asyncSetUp(self):
        self.app = make_app()
        self.app.bot_data.update(engine_started=True, auto_on=False, jobq=asyncio.PriorityQueue(), sendq=asyncio.Queue())
        self.ctx = lambda args=(): SimpleNamespace(application=self.app, args=list(args))

    async def test_buttons_admin_only_and_toggle(self):
        u, _, sent, ans, edits = fake_update(uid=999, data="eng:start")      # not an admin
        await run.on_engine_button(u, self.ctx())
        self.assertFalse(self.app.bot_data["auto_on"]); self.assertTrue(ans[0][1])
        u, _, sent, ans, edits = fake_update(uid=1, data="eng:start")
        await run.on_engine_button(u, self.ctx())
        self.assertTrue(self.app.bot_data["auto_on"]); self.assertIn("RUNNING", edits[-1])
        u, _, sent, ans, edits = fake_update(uid=1, data="eng:stop")
        self.app.bot_data["jobq"].put_nowait((-60, 1, 1, "EUR/USD", "1min"))
        await run.on_engine_button(u, self.ctx())
        self.assertFalse(self.app.bot_data["auto_on"]); self.assertIn("STOPPED", edits[-1])
        self.assertTrue(self.app.bot_data["jobq"].empty())                     # queued jobs discarded on STOP
        u, _, sent, ans, edits = fake_update(uid=1, data="eng:status")
        await run.on_engine_button(u, self.ctx()); self.assertEqual(ans[0][0], "Refreshed.")

    async def test_commands(self):
        for fn, args, expect, state in ((run.startengine_cmd, (), "STARTED", True), (run.stopengine_cmd, (), "STOPPED", False),
                                        (run.autosignal_cmd, ("on",), "STARTED", True), (run.autosignal_cmd, ("off",), "STOPPED", False)):
            u, _, sent, *_ = fake_update(uid=1, args=args)
            await fn(u, self.ctx(args))
            self.assertIn(expect, sent[0][0]); self.assertIs(sent[0][1], run.ENGINE_KB)
            self.assertEqual(self.app.bot_data["auto_on"], state)
        u, _, sent, *_ = fake_update(uid=999)                                  # non-admin: silently ignored
        await run.engine_cmd(u, self.ctx()); await run.startengine_cmd(u, self.ctx()); self.assertEqual(sent, [])
        u, _, sent, *_ = fake_update(uid=1)
        await run.engine_cmd(u, self.ctx())
        self.assertIn("START ENGINE", [b.text for b in sent[0][1].inline_keyboard[0]][0])
        self.assertIn("STOP ENGINE", [b.text for b in sent[0][1].inline_keyboard[0]][1])

    async def test_no_api_key_and_not_started(self):
        run.TD_KEY = ""
        self.assertIn("TWELVE_DATA_API_KEY", await run.set_engine(self.app, True))
        run.TD_KEY = "k"; self.app.bot_data["engine_started"] = False
        self.assertIn("still starting", await run.set_engine(self.app, True))

    async def test_status_warnings(self):
        txt = await run.status_text(self.app)
        self.assertIn("STOPPED", txt); self.assertIn("EUR/USD", txt); self.assertIn("1M", txt); self.assertIn("5M", txt)
        self.assertIn("7.2 requests/min", txt)
        self.assertIn("800/day budget lasts", txt)                              # free-plan reality check is shown
        run.PAIRS = [f"AAA/B{c}" for c in "ABCDEFGHIJ"]
        self.assertIn("need more than 8 requests/min", await run.status_text(self.app))

    async def test_autosignal_now_and_test(self):
        call = find("CALL")
        self.scenario = lambda p, t, n: closed_series(n, t, call, 0.00006)
        run.PAIRS = ["EUR/USD", "GBP/USD"]
        u, _, sent, *_ = fake_update(uid=1, args=("now",))
        await run.autosignal_cmd(u, self.ctx(("now",)))
        for _ in range(50):
            await asyncio.sleep(0.1)
            if self.app.bot.messages: break
        self.assertIn("Scan result", self.app.bot.messages[-1][1]); self.assertIn("signal(s) queued", self.app.bot.messages[-1][1])
        self.assertEqual(self.app.bot_data["sendq"].qsize(), 4)                # forced scan: 2 pairs x 2 timeframes
        u, _, sent, *_ = fake_update(uid=1, args=("test", "EURUSD", "5m"))
        await run.autosignal_cmd(u, self.ctx(("test", "EURUSD", "5m")))
        for _ in range(50):
            await asyncio.sleep(0.1)
            if self.app.bot.photos: break
        self.assertEqual([p[0] for p in self.app.bot.photos], [1])             # test signal goes to the admin only
        self.assertIn("TEST SIGNAL", self.app.bot.photos[0][2]); self.assertIn("Timeframe: 5M", self.app.bot.photos[0][2])
        u, _, sent, *_ = fake_update(uid=1, args=("test", "EURUSD", "15m"))
        await run.autosignal_cmd(u, self.ctx(("test", "EURUSD", "15m"))); self.assertIn("Usage", sent[0][0])

    async def test_welcome_entry_adds_admin_hint_only_for_admins(self):
        async def fake_welcome(update, context): pass
        orig = run.welcome; run.welcome = fake_welcome
        try:
            u, _, sent, *_ = fake_update(uid=1)
            await run.welcome_entry(u, SimpleNamespace(application=self.app)); self.assertIn("/engine", sent[0][0])
            u, _, sent, *_ = fake_update(uid=999)
            await run.welcome_entry(u, SimpleNamespace(application=self.app)); self.assertEqual(sent, [])
        finally:
            run.welcome = orig


def tearDownModule():
    shutil.rmtree(WORK, ignore_errors=True)


if __name__ == "__main__":
    print(f"stand-in libraries used: {STUBBED} | originals: {ORIG_DIR}")
    unittest.main(verbosity=2)
