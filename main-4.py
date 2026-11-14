"""HONESTY AI VIP BOT - everything in ONE file.  Start with:  python main.py

Telegram bot  : users send a Trader ID (/verify); admins in ADMIN_CHAT_IDS get Approve / Reject buttons.
HTTP endpoints: GET /  GET /health  GET|POST /<POSTBACK_SECRET>/postback  (partner postbacks)  POST /tg/<hash> (Telegram).
Admin commands: /pending /stats /events /signal <text> (manual broadcast to approved users),
/analysis <symbol> [interval] (admin-only indicator readings; never sent to users).      Verification is MANUAL: no broker API is called.

Install : pip install "python-telegram-bot[webhooks]==21.6" "asyncpg==0.29.0"
Start   : python main.py
Env     : TELEGRAM_BOT_TOKEN (required)  ADMIN_CHAT_IDS (required, numeric, comma-separated)
          DATABASE_URL  POSTBACK_SECRET  MIN_DEPOSIT  DEPOSIT_CURRENCY  VIP_INVITE_LINK  AUTO_APPROVE
          TRADER_ID_PATTERN  FORCE_POLLING  LOG_LEVEL        (PORT / RENDER_EXTERNAL_URL are set by Render)
Render sets RENDER_EXTERNAL_URL -> webhook mode.  Anywhere else -> long polling.
Old variables (SECRET_KEY, API_KEYS, ADMIN_PASSWORD_HASH, ...) are not used any more and are ignored.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import signal
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Awaitable, Callable, Mapping, Optional
from urllib.parse import parse_qsl

import httpx  # installed together with python-telegram-bot
import tornado.web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters,
)

logger = logging.getLogger("honesty-bot")


# ==========================================================================
# SETTINGS
# ==========================================================================
@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    postback_secret: str = field(repr=False)
    admin_ids: frozenset[int]
    database_url: str
    webhook_base_url: str
    port: int
    trader_id_re: "re.Pattern[str]"
    min_deposit: str
    currency: str
    vip_link: str
    force_polling: bool
    auto_approve: bool

    @property
    def min_deposit_value(self) -> Optional[Decimal]:
        try:
            v = Decimal(self.min_deposit)
        except (InvalidOperation, ValueError):
            return None
        return v if v > 0 else None

    @property
    def use_webhook(self) -> bool:
        return bool(self.webhook_base_url) and not self.force_polling

    @property
    def webhook_path(self) -> str:
        # Derived from the token so it cannot be guessed; no extra variable needed.
        return "tg/" + hashlib.sha256(("path:" + self.token).encode()).hexdigest()[:32]

    @property
    def webhook_secret(self) -> str:
        # Telegram sends this in a header; only [A-Za-z0-9_-] is allowed (hex is fine).
        return hashlib.sha256(("secret:" + self.token).encode()).hexdigest()

    def is_admin(self, user_id: Optional[int], chat_id: Optional[int] = None) -> bool:
        return (user_id in self.admin_ids) or (chat_id is not None and chat_id in self.admin_ids)


def _parse_ids(raw: str) -> frozenset[int]:
    out = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            try:
                out.add(int(part))
            except ValueError:
                raise SystemExit(f"ADMIN_CHAT_IDS contains a non-numeric value: {part!r}") from None
    return frozenset(out)


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set (get it from @BotFather)")
    admins = _parse_ids(env.get("ADMIN_CHAT_IDS", ""))
    if not admins:
        raise SystemExit("ADMIN_CHAT_IDS is not set (numeric Telegram IDs of your admins, comma-separated)")
    try:
        trader_re = re.compile(env.get("TRADER_ID_PATTERN", r"^[A-Za-z0-9_-]{3,32}$"))
    except re.error as exc:
        raise SystemExit(f"TRADER_ID_PATTERN is not a valid regex: {exc}") from None
    db = env.get("DATABASE_URL", "").strip()
    if db.startswith("postgresql+asyncpg://"):
        db = "postgresql://" + db[len("postgresql+asyncpg://"):]
    if db.startswith("postgres://"):
        db = "postgresql://" + db[len("postgres://"):]
    try:
        port = int(env.get("PORT", "10000"))
    except ValueError:
        port = 10000
    return Settings(
        token=token,
        postback_secret=env.get("POSTBACK_SECRET", "").strip(),
        admin_ids=admins,
        database_url=db,
        webhook_base_url=(env.get("WEBHOOK_URL") or env.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/"),
        port=port,
        trader_id_re=trader_re,
        min_deposit=env.get("MIN_DEPOSIT", "").strip(),
        currency=env.get("DEPOSIT_CURRENCY", "USD").strip() or "USD",
        vip_link=env.get("VIP_INVITE_LINK", "").strip(),
        force_polling=env.get("FORCE_POLLING", "").strip().lower() in {"1", "true", "yes"},
        auto_approve=env.get("AUTO_APPROVE", "").strip().lower() in {"1", "true", "yes"},
    )


# ==========================================================================
# STORAGE  (PostgreSQL via asyncpg, or in-memory fallback)
# ==========================================================================
PENDING, APPROVED, REJECTED = "pending", "approved", "rejected"


class Taken(Exception):
    """This Trader ID is already linked to a different Telegram user."""


class MemoryStore:
    def __init__(self) -> None:
        self._rows: dict[int, dict] = {}
        self._events: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    async def init(self) -> None: ...
    async def close(self) -> None: ...

    async def get_by_user(self, tg_id: int) -> Optional[dict]:
        r = self._rows.get(tg_id)
        return dict(r) if r else None

    async def get_by_trader(self, trader_id: str) -> Optional[dict]:
        for r in self._rows.values():
            if r["trader_id"] == trader_id:
                return dict(r)
        return None

    async def upsert_submission(self, tg_id: int, username: Optional[str], trader_id: str) -> dict:
        async with self._lock:
            for uid, r in self._rows.items():
                if r["trader_id"] == trader_id and uid != tg_id:
                    raise Taken()
            row = {"telegram_id": tg_id, "username": username, "trader_id": trader_id, "status": PENDING,
                   "submitted_at": datetime.now(timezone.utc), "decided_at": None, "decided_by": None}
            self._rows[tg_id] = row
            return dict(row)

    async def decide(self, trader_id: str, status: str, admin_id: int) -> Optional[dict]:
        """Atomic: only a still-pending row can be decided. Returns the row, or None."""
        async with self._lock:
            for r in self._rows.values():
                if r["trader_id"] == trader_id and r["status"] == PENDING:
                    r.update(status=status, decided_at=datetime.now(timezone.utc), decided_by=admin_id)
                    return dict(r)
        return None

    async def list_pending(self, limit: int = 10) -> list[dict]:
        rows = sorted((r for r in self._rows.values() if r["status"] == PENDING), key=lambda r: r["submitted_at"])
        return [dict(r) for r in rows[:limit]]

    async def counts(self) -> dict[str, int]:
        out = {PENDING: 0, APPROVED: 0, REJECTED: 0}
        for r in self._rows.values():
            out[r["status"]] += 1
        return out

    async def list_approved_ids(self) -> list[int]:
        return [r["telegram_id"] for r in self._rows.values() if r["status"] == APPROVED]

    async def add_event(self, ev: dict) -> bool:
        """True if new, False if this event_id was already recorded."""
        async with self._lock:
            if ev["event_id"] in self._events:
                return False
            self._events[ev["event_id"]] = {**ev, "received_at": datetime.now(timezone.utc)}
            return True

    async def trader_summary(self, trader_id: str) -> Optional[dict]:
        evs = [e for e in self._events.values() if e.get("trader_id") == trader_id]
        if not evs:
            return None
        deps = [e for e in evs if e.get("kind") in ("ftd", "dep")]
        return {"registered": any(e.get("kind") == "reg" for e in evs), "ftd": any(e.get("kind") == "ftd" for e in evs),
                "deposits": sum((e.get("amount") or Decimal(0) for e in deps), Decimal(0)),
                "deposit_count": len(deps), "events": len(evs)}

    async def list_events(self, limit: int = 10) -> list[dict]:
        rows = sorted(self._events.values(), key=lambda r: r["received_at"], reverse=True)
        return [dict(r) for r in rows[:limit]]


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS bot_submissions (
        telegram_id  BIGINT PRIMARY KEY,
        username     TEXT,
        trader_id    TEXT NOT NULL UNIQUE,
        status       TEXT NOT NULL DEFAULT 'pending',
        submitted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        decided_at   TIMESTAMPTZ,
        decided_by   BIGINT)""",
    """CREATE TABLE IF NOT EXISTS bot_events (
        event_id    TEXT PRIMARY KEY,
        event_name  TEXT,
        click_id    TEXT,
        site_id     TEXT,
        link_id     TEXT,
        received_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    "ALTER TABLE bot_events ADD COLUMN IF NOT EXISTS kind TEXT",
    "ALTER TABLE bot_events ADD COLUMN IF NOT EXISTS trader_id TEXT",
    "ALTER TABLE bot_events ADD COLUMN IF NOT EXISTS amount NUMERIC(18,2)",
    "CREATE INDEX IF NOT EXISTS bot_events_trader_idx ON bot_events (trader_id)",
)


class PostgresStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool = None

    async def init(self) -> None:
        import asyncpg  # imported lazily so local tests do not need it

        last: Exception | None = None
        for attempt in range(1, 3):  # the database may still be starting right after a deploy
            try:
                self._pool = await asyncpg.create_pool(
                    self._dsn, min_size=1, max_size=4, command_timeout=15, timeout=10)
                break
            except Exception as exc:  # noqa: BLE001
                last = exc
                await asyncio.sleep(3 * attempt)
        if self._pool is None:
            raise RuntimeError(f"database not reachable: {type(last).__name__}")
        async with self._pool.acquire() as con:
            for stmt in _SCHEMA:
                await con.execute(stmt)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    async def get_by_user(self, tg_id: int) -> Optional[dict]:
        row = await self._pool.fetchrow("SELECT * FROM bot_submissions WHERE telegram_id=$1", tg_id)
        return dict(row) if row else None

    async def get_by_trader(self, trader_id: str) -> Optional[dict]:
        row = await self._pool.fetchrow("SELECT * FROM bot_submissions WHERE trader_id=$1", trader_id)
        return dict(row) if row else None

    async def upsert_submission(self, tg_id: int, username: Optional[str], trader_id: str) -> dict:
        import asyncpg

        try:
            row = await self._pool.fetchrow(
                """INSERT INTO bot_submissions (telegram_id, username, trader_id, status)
                   VALUES ($1,$2,$3,'pending')
                   ON CONFLICT (telegram_id) DO UPDATE
                     SET username=EXCLUDED.username, trader_id=EXCLUDED.trader_id, status='pending',
                         submitted_at=now(), decided_at=NULL, decided_by=NULL
                   RETURNING *""",
                tg_id, username, trader_id)
        except asyncpg.UniqueViolationError:
            raise Taken() from None
        return dict(row)

    async def decide(self, trader_id: str, status: str, admin_id: int) -> Optional[dict]:
        row = await self._pool.fetchrow(
            """UPDATE bot_submissions SET status=$2, decided_at=now(), decided_by=$3
               WHERE trader_id=$1 AND status='pending' RETURNING *""",
            trader_id, status, admin_id)
        return dict(row) if row else None

    async def list_pending(self, limit: int = 10) -> list[dict]:
        rows = await self._pool.fetch(
            "SELECT * FROM bot_submissions WHERE status='pending' ORDER BY submitted_at LIMIT $1", limit)
        return [dict(r) for r in rows]

    async def counts(self) -> dict[str, int]:
        rows = await self._pool.fetch("SELECT status, count(*) AS n FROM bot_submissions GROUP BY status")
        out = {PENDING: 0, APPROVED: 0, REJECTED: 0}
        out.update({r["status"]: r["n"] for r in rows})
        return out

    async def list_approved_ids(self) -> list[int]:
        rows = await self._pool.fetch("SELECT telegram_id FROM bot_submissions WHERE status='approved'")
        return [r["telegram_id"] for r in rows]

    async def add_event(self, ev: dict) -> bool:
        row = await self._pool.fetchrow(
            """INSERT INTO bot_events (event_id, event_name, click_id, site_id, link_id, kind, trader_id, amount)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT (event_id) DO NOTHING RETURNING event_id""",
            ev["event_id"], ev.get("event_name"), ev.get("click_id"), ev.get("site_id"), ev.get("link_id"),
            ev.get("kind"), ev.get("trader_id"), ev.get("amount"))
        return row is not None

    async def trader_summary(self, trader_id: str) -> Optional[dict]:
        r = await self._pool.fetchrow(
            """SELECT count(*) AS n,
                      coalesce(bool_or(kind='reg'), false) AS reg,
                      coalesce(bool_or(kind='ftd'), false) AS ftd,
                      coalesce(sum(amount) FILTER (WHERE kind IN ('ftd','dep')), 0) AS dep_total,
                      count(*) FILTER (WHERE kind IN ('ftd','dep')) AS dep_n
               FROM bot_events WHERE trader_id=$1""", trader_id)
        if not r or r["n"] == 0:
            return None
        return {"registered": r["reg"], "ftd": r["ftd"], "deposits": Decimal(r["dep_total"]),
                "deposit_count": r["dep_n"], "events": r["n"]}

    async def list_events(self, limit: int = 10) -> list[dict]:
        rows = await self._pool.fetch("SELECT * FROM bot_events ORDER BY received_at DESC LIMIT $1", limit)
        return [dict(r) for r in rows]


# ==========================================================================
# BUSINESS RULES  (no Telegram / HTTP here, so they are easy to test)
# ==========================================================================
@dataclass
class SubmitResult:
    code: str  # invalid | taken | already_approved | already_pending | submitted
    row: Optional[dict] = None


async def submit_trader_id(store, settings: Settings, tg_id: int, username: Optional[str], raw: str) -> SubmitResult:
    trader_id = (raw or "").strip()
    if not settings.trader_id_re.match(trader_id):
        return SubmitResult("invalid")
    mine = await store.get_by_user(tg_id)
    if mine and mine["status"] == APPROVED:
        return SubmitResult("already_approved", mine)
    if mine and mine["status"] == PENDING:
        return SubmitResult("already_pending", mine)
    other = await store.get_by_trader(trader_id)
    if other and other["telegram_id"] != tg_id:
        return SubmitResult("taken")
    try:
        row = await store.upsert_submission(tg_id, username, trader_id)  # also re-submits after a rejection
    except Taken:
        return SubmitResult("taken")
    return SubmitResult("submitted", row)


async def decide(store, trader_id: str, approve: bool, admin_id: int) -> Optional[dict]:
    return await store.decide(trader_id, APPROVED if approve else REJECTED, admin_id)


def meets_minimum(settings: Settings, summary: Optional[dict]) -> bool:
    mv = settings.min_deposit_value
    return bool(mv is not None and summary and summary["deposit_count"] > 0 and summary["deposits"] >= mv)


def describe_summary(settings: Settings, summary: Optional[dict]) -> str:
    if not summary:
        return "Postback data: none for this Trader ID (not registered via our link, or no postback received yet)."
    line = ("Postback data: registered via our link: " + ("yes" if summary["registered"] else "not seen")
            + f"; first deposit: {'yes' if summary['ftd'] else 'no'}"
            + f"; deposits: {summary['deposits']:.2f} {settings.currency} ({summary['deposit_count']})")
    mv = settings.min_deposit_value
    if mv is not None:
        line += f"\nMeets minimum ({mv} {settings.currency}): {'YES' if meets_minimum(settings, summary) else 'no'}"
    return line


async def try_auto_approve(store, settings: Settings, trader_id: str) -> Optional[dict]:
    """Only when AUTO_APPROVE is on, the request is still pending and postback deposits reach MIN_DEPOSIT."""
    if not settings.auto_approve:
        return None
    pending = await store.get_by_trader(trader_id)
    if not pending or pending["status"] != PENDING:
        return None
    if not meets_minimum(settings, await store.trader_summary(trader_id)):
        return None
    return await store.decide(trader_id, APPROVED, 0)  # decided_by 0 = automatic


# ---- manual signal broadcast ----------------------------------------------
SIGNAL_FOOTER = ("\n\n-- Risk warning: trading carries a high risk of losing money, including your whole deposit. "
                 "This message is not financial advice and does not guarantee any result.")
MAX_SIGNAL_CHARS = 3000


async def broadcast(bot, user_ids: list[int], text: str, delay: float = 0.05) -> tuple[int, int]:
    """Send `text` to every id. Returns (sent, failed). Blocked/deleted users just count as failed."""
    sent = failed = 0
    for uid in user_ids:
        try:
            await bot.send_message(uid, text)
            sent += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.warning("signal not delivered to %s: %s", uid, type(exc).__name__)
        await asyncio.sleep(delay)  # stay under Telegram's ~30 messages/second limit
    return sent, failed


# ---- admin-only market analysis (informational indicator readings) ---------
ANALYSIS_FOOTER = ("Informational indicator readings from delayed public data (Yahoo Finance). Not a prediction, "
                   "not advice; prices may differ from your broker's.")
_INTERVALS = {"1m": "1d", "5m": "5d", "15m": "5d", "1h": "1mo", "1d": "6mo"}  # interval -> history range
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9=\-\.\^]{3,15}$")
_CRYPTO = {"BTC", "ETH", "LTC", "XRP", "SOL", "DOGE", "BNB", "ADA"}


def yahoo_symbol(raw: str) -> Optional[str]:
    """EURUSD -> EURUSD=X, BTCUSD -> BTC-USD; anything already in Yahoo form is kept."""
    sym = (raw or "").strip().upper()
    if not _SYMBOL_RE.match(sym):
        return None
    if any(c in sym for c in "=-.^"):
        return sym
    if len(sym) == 6 and sym[:3] in _CRYPTO and sym[3:] in {"USD", "USDT"}:
        return f"{sym[:3]}-USD"
    if len(sym) == 6 and sym.isalpha():
        return sym + "=X"
    return sym


def sma(values: list[float], n: int) -> Optional[float]:
    return sum(values[-n:]) / n if len(values) >= n else None


def ema_series(values: list[float], n: int) -> list[float]:
    if len(values) < n:
        return []
    k = 2 / (n + 1)
    out = [sum(values[:n]) / n]
    for v in values[n:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(values: list[float], n: int = 14) -> Optional[float]:
    if len(values) < n + 1:
        return None
    deltas = [b - a for a, b in zip(values, values[1:])]
    gain = sum(max(d, 0) for d in deltas[:n]) / n
    loss = sum(max(-d, 0) for d in deltas[:n]) / n
    for d in deltas[n:]:  # Wilder smoothing
        gain = (gain * (n - 1) + max(d, 0)) / n
        loss = (loss * (n - 1) + max(-d, 0)) / n
    if gain == 0 and loss == 0:
        return 50.0
    return 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)


def macd(values: list[float]) -> Optional[tuple[float, float, float]]:
    """(macd, signal, histogram) with the standard 12/26/9 settings."""
    fast, slow = ema_series(values, 12), ema_series(values, 26)
    if not slow:
        return None
    line = [f - sl for f, sl in zip(fast[len(fast) - len(slow):], slow)]
    sig = ema_series(line, 9)
    if not sig:
        return None
    return line[-1], sig[-1], line[-1] - sig[-1]


def parse_yahoo_closes(data: dict) -> list[float]:
    try:
        closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError):
        return []
    return [float(c) for c in closes if isinstance(c, (int, float))]


def analysis_text(symbol: str, interval: str, closes: list[float]) -> str:
    if len(closes) < 35:
        return f"Not enough data for {symbol} ({interval}): got {len(closes)} candles, need at least 35."
    price = closes[-1]
    lines = [f"{symbol} - {interval} candles ({len(closes)} used)", f"Last price: {price:.5f}"]
    r = rsi(closes)
    if r is not None:
        zone = "overbought zone" if r >= 70 else "oversold zone" if r <= 30 else "neutral zone"
        lines.append(f"RSI(14): {r:.1f} ({zone})")
    mc = macd(closes)
    if mc:
        lines.append(f"MACD histogram: {mc[2]:+.5f} (MACD {'above' if mc[2] > 0 else 'below'} its signal line)")
    for n in (20, 50):
        v = sma(closes, n)
        if v is not None:
            lines.append(f"SMA({n}): {v:.5f} (price {'above' if price > v else 'below'})")
    return "\n".join(lines) + "\n\n" + ANALYSIS_FOOTER


async def fetch_closes(symbol: str, interval: str) -> list[float]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as client:
        resp = await client.get(url, params={"interval": interval, "range": _INTERVALS[interval]})
    resp.raise_for_status()
    return parse_yahoo_closes(resp.json())


# ---- partner postbacks (Quotex affiliate "postback" format) -----------------
_ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,128}$")
_AMOUNT_RE = re.compile(r"^\d{1,12}(\.\d{1,6})?$")
MAX_BODY = 16384
KIND_LABEL = {"reg": "Registration", "conf": "Email confirmed", "ftd": "First deposit",
              "dep": "Repeat deposit", "withdrawal": "Withdrawal"}
_KIND_ALIASES = {
    "reg": "reg", "registration": "reg", "registered": "reg",
    "conf": "conf", "confirmed": "conf", "email_confirmed": "conf", "email_confirmation": "conf",
    "ftd": "ftd", "first_deposit": "ftd",
    "dep": "dep", "deposit": "dep", "repeat_deposit": "dep", "not_first_deposit": "dep",
    "withdrawal": "withdrawal", "withdraw": "withdrawal",
}


def _header(headers: Mapping[str, str], name: str) -> str:
    low = name.lower()
    for k, v in headers.items():
        if k.lower() == low:
            return v
    return ""


def parse_params(query: Mapping[str, str], body: bytes, content_type: str) -> dict[str, str]:
    """Merge query-string, form and JSON parameters into one flat str->str dict."""
    out: dict[str, str] = dict(query)
    if body:
        if "json" in (content_type or "").lower():
            try:
                data = json.loads(body)
            except (ValueError, RecursionError):
                data = None
            if isinstance(data, dict):
                out.update({str(k): str(v) for k, v in list(data.items())[:50] if not isinstance(v, (dict, list))})
        else:
            try:
                out.update(dict(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=False)[:50]))
            except ValueError:
                pass
    return out


def _first(params: Mapping[str, str], names: tuple[str, ...], rx: "re.Pattern[str]") -> Optional[str]:
    """First parameter among `names` that is present AND valid (unresolved macros like "{event_id}" are skipped)."""
    for n in names:
        v = (params.get(n) or "").strip()
        if v and rx.match(v):
            return v
    return None


def event_kind(params: Mapping[str, str]) -> Optional[str]:
    """reg / conf / ftd / dep / withdrawal, from `status=<name>` or from flags such as `ftd=true`."""
    kind = _KIND_ALIASES.get((params.get("status") or "").strip().lower().replace(" ", "_"))
    if kind:
        return kind
    for k in ("ftd", "dep", "withdrawal", "conf", "reg"):  # most specific first
        if (params.get(k) or "").strip().lower() in {"true", "1", "yes"}:
            return k
    return None


def parse_amount(raw: Optional[str]) -> Optional[Decimal]:
    raw = (raw or "").strip()
    if not _AMOUNT_RE.match(raw):
        return None
    return Decimal(raw).quantize(Decimal("0.01"))


def build_event(params: Mapping[str, str]) -> dict:
    """Accepts both the partner's own names (click_id, event_id, site_id, lid, trader_id, sumdep) and the tracker
    names from its docs (subid, sub_id_1..4, payout). Stores tracking fields, the Trader ID and the amount.
    Country, user agent, e-mail etc. are never stored."""
    kind = event_kind(params)
    trader = _first(params, ("trader_id", "sub_id_4"), _ID_RE)
    ev = {
        "event_id": _first(params, ("event_id", "sub_id_1"), _ID_RE),
        "kind": kind,
        "event_name": KIND_LABEL.get(kind, (params.get("status") or "event")[:40]) if kind else
                      re.sub(r"[^A-Za-z0-9_ .:-]", "", (params.get("status") or "event"))[:40] or "event",
        "click_id": _first(params, ("click_id", "subid"), _ID_RE),
        "site_id": _first(params, ("site_id", "sub_id_2"), _ID_RE),
        "link_id": _first(params, ("lid", "link_id", "sub_id_3"), _ID_RE),
        "trader_id": None if trader in (None, "0") else trader,
        "amount": parse_amount(params.get("sumdep") or params.get("payout")),
    }
    if not ev["event_id"]:  # partner sent no usable ID: identical resent postbacks still de-duplicate
        canon = json.dumps(sorted(params.items()), separators=(",", ":"))
        ev["event_id"] = "h-" + hashlib.sha256(canon.encode()).hexdigest()[:32]
    return ev


def format_event_message(ev: dict, currency: str = "USD") -> str:
    lines = ["HONESTY AI VIP SERVER", "", f"Event: {ev.get('event_name') or 'event'}"]
    if ev.get("trader_id"):
        lines.append(f"Trader ID: {ev['trader_id']}")
    if ev.get("amount") is not None:
        lines.append(f"Amount: {ev['amount']:.2f} {currency}")
    lines += [f"Event ID: {ev['event_id']}", f"Timestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"]
    return "\n".join(lines)


async def process_postback(
    settings: Settings, store, notify: Callable[[str], Awaitable[None]],
    headers: Mapping[str, str], query: Mapping[str, str], body: bytes,
    path_key: str = "", after: Optional[Callable[[dict], Awaitable[None]]] = None,
) -> tuple[int, dict]:
    """Returns (http_status, json_body).

    URL the partner calls:  https://<service>/<POSTBACK_SECRET>/postback?status={status}&...
    (also accepted: header X-Webhook-Secret, or ?key=). Without POSTBACK_SECRET the endpoint only acknowledges
    (parameter NAMES are logged, nothing is stored, no alert) so a public URL cannot be used to spam you.
    With the secret: each event_id is stored once (so a resent postback never double-counts a deposit).
    """
    if len(body) > MAX_BODY:
        return 413, {"status": "too_large"}
    params = parse_params(query, body, _header(headers, "Content-Type"))
    supplied = path_key or _header(headers, "X-Webhook-Secret") or params.get("key", "")
    params.pop("key", None)
    logger.info("postback received; parameter names=%s", sorted(params.keys()))
    if not settings.postback_secret:
        return 200, {"status": "received", "stored": False}
    if not supplied or not hmac.compare_digest(supplied.encode(), settings.postback_secret.encode()):
        return 401, {"status": "unauthorized"}
    if store is None:
        return 503, {"status": "starting"}
    ev = build_event(params)
    created = await store.add_event(ev)
    if created:
        await notify(format_event_message(ev, settings.currency))
        if after is not None:
            await after(ev)
    return 200, {"status": "accepted", "duplicate": not created, "event_id": ev["event_id"]}


# ==========================================================================
# TELEGRAM HANDLERS
# ==========================================================================
AWAITING = "awaiting_trader_id"


def _s(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def _store(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data["store"]


def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user, chat = update.effective_user, update.effective_chat
    return _s(context).is_admin(user.id if user else None, chat.id if chat else None)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = _s(context)
    deposit = f"\nMinimum deposit: {s.min_deposit} {s.currency}." if s.min_deposit else ""
    admin = "\n\nAdmin: /pending /stats /events /trader <id> /signal <text> /analysis <symbol>" if _is_admin(update, context) else ""
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Submit my Trader ID", callback_data="submit")]])
    await update.effective_message.reply_text(
        "Welcome to HONESTY AI VIP.\n\n"
        "To request VIP access, send your Trader ID. An admin checks it manually and the result is sent to you here."
        f"{deposit}\n\n"
        "/verify - submit your Trader ID\n/status - check your request\n/help - show this message" + admin,
        reply_markup=kb,
    )


async def on_submit_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    context.user_data[AWAITING] = True
    await q.message.reply_text("Send your Trader ID as a message now.")


async def verify_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        await _submit(update, context, context.args[0])
    else:
        context.user_data[AWAITING] = True
        await update.effective_message.reply_text("Send your Trader ID as a message now.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get(AWAITING):
        await _submit(update, context, update.effective_message.text or "")
    else:
        await update.effective_message.reply_text("Use /verify to submit your Trader ID, or /help.")


async def _submit(update: Update, context: ContextTypes.DEFAULT_TYPE, raw: str) -> None:
    s, user, msg = _s(context), update.effective_user, update.effective_message
    res = await submit_trader_id(_store(context), s, user.id, user.username, raw)
    if res.code == "invalid":
        context.user_data[AWAITING] = True  # let them retry
        await msg.reply_text("That does not look like a valid Trader ID (3-32 letters, digits, - or _). Please try again.")
        return
    context.user_data.pop(AWAITING, None)
    if res.code == "taken":
        await msg.reply_text("This Trader ID is already linked to another Telegram account. Contact an admin if this is a mistake.")
    elif res.code == "already_approved":
        await msg.reply_text(f"You are already approved (Trader ID {res.row['trader_id']}).")
    elif res.code == "already_pending":
        await msg.reply_text(f"Your request for Trader ID {res.row['trader_id']} is already waiting for an admin. Please be patient.")
    else:
        auto = await try_auto_approve(_store(context), s, res.row["trader_id"])
        if auto:
            await msg.reply_text(approval_text(s, auto))
            await notify_admins(context.bot, s, f"Auto-approved (postback deposits reach the minimum)\n{_admin_text(auto)}")
            return
        card = await admin_card(_store(context), s, res.row)
        delivered = await notify_admins(context.bot, s, card, _admin_keyboard(res.row["trader_id"]))
        if delivered:
            await msg.reply_text("Thank you. Your Trader ID was sent to the admins for checking. You will get a message here with the result.")
        else:
            await msg.reply_text("Your Trader ID was saved, but I could not reach the admins right now. They will see it later with /pending.")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    row = await _store(context).get_by_user(update.effective_user.id)
    if not row:
        text = "You have not submitted a Trader ID yet. Use /verify."
    elif row["status"] == APPROVED:
        text = f"Approved (Trader ID {row['trader_id']})."
    elif row["status"] == REJECTED:
        text = f"Rejected (Trader ID {row['trader_id']}). You can submit a corrected Trader ID with /verify."
    else:
        text = f"Waiting for an admin (Trader ID {row['trader_id']})."
    await update.effective_message.reply_text(text)


def _admin_text(row: dict, extra: str = "") -> str:
    who = f"@{row['username']}" if row.get("username") else "(no username)"
    text = f"New VIP request\nTrader ID: {row['trader_id']}\nTelegram: {who}\nTelegram ID: {row['telegram_id']}"
    return text + (f"\n\n{extra}" if extra else "")


async def admin_card(store, settings: Settings, row: dict) -> str:
    return _admin_text(row, describe_summary(settings, await store.trader_summary(row["trader_id"])))


def approval_text(s: Settings, row: dict) -> str:
    text = f"Your Trader ID {row['trader_id']} was approved."
    return text + (f"\nVIP access: {s.vip_link}" if s.vip_link else "\nAn admin will send your VIP access shortly.")


def _admin_keyboard(trader_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Approve", callback_data=f"ok:{trader_id}"),
        InlineKeyboardButton("Reject", callback_data=f"no:{trader_id}"),
    ]])


async def notify_admins(bot, settings: Settings, text: str, markup=None) -> int:
    delivered = 0
    for chat_id in settings.admin_ids:
        try:
            await bot.send_message(chat_id, text, reply_markup=markup)
            delivered += 1
        except Exception as exc:  # noqa: BLE001 - e.g. admin never pressed /start; log the type only (no token)
            logger.warning("could not notify admin %s: %s", chat_id, type(exc).__name__)
    return delivered


async def on_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    s = _s(context)
    chat_id = q.message.chat.id if q.message else None
    if not s.is_admin(q.from_user.id, chat_id):
        await q.answer("Only admins can do this.", show_alert=True)
        return
    action, trader_id = q.data.split(":", 1)
    approve = action == "ok"
    row = await decide(_store(context), trader_id, approve, q.from_user.id)
    if row is None:
        await q.answer("Already handled by someone else, or not found.", show_alert=True)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass
        return
    await q.answer("Saved.")
    label = "APPROVED" if approve else "REJECTED"
    old = getattr(q.message, "text", None) or _admin_text(row)
    try:
        await q.edit_message_text(f"{old}\n\n{label} by {q.from_user.full_name}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not edit admin message: %s", type(exc).__name__)
    if approve:
        user_text = approval_text(s, row)
    else:
        user_text = f"Your Trader ID {row['trader_id']} was not approved. Check it and submit again with /verify, or contact an admin."
    try:
        await context.bot.send_message(row["telegram_id"], user_text)
    except Exception as exc:  # noqa: BLE001 - user may have blocked the bot
        logger.warning("could not message user %s: %s", row["telegram_id"], type(exc).__name__)
        if q.message:
            await context.bot.send_message(q.message.chat.id, f"Decision saved, but I could not message user {row['telegram_id']}.")


async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update, context):
        return
    rows = await _store(context).list_pending(10)
    if not rows:
        await update.effective_message.reply_text("No pending requests.")
        return
    for row in rows:
        card = await admin_card(_store(context), _s(context), row)
        await update.effective_message.reply_text(card, reply_markup=_admin_keyboard(row["trader_id"]))


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update, context):
        return
    c = await _store(context).counts()
    await update.effective_message.reply_text(f"Pending: {c[PENDING]}\nApproved: {c[APPROVED]}\nRejected: {c[REJECTED]}")


async def events_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update, context):
        return
    rows = await _store(context).list_events(10)
    if not rows:
        await update.effective_message.reply_text("No postback events stored yet.")
        return
    lines = [f"{r['received_at']:%Y-%m-%d %H:%M} | {r.get('event_name') or '-'} | trader {r.get('trader_id') or '-'}"
             + (f" | {r['amount']:.2f}" if r.get("amount") is not None else "") for r in rows]
    await update.effective_message.reply_text("Latest events:\n" + "\n".join(lines))


async def trader_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/trader <TraderID> - admin only: postback data + Telegram request status for one Trader ID."""
    if not _is_admin(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /trader <TraderID>")
        return
    tid, st, s = context.args[0].strip(), _store(context), _s(context)
    row = await st.get_by_trader(tid)
    req = (f"Telegram request: {row['status']} (user {row['telegram_id']}"
           + (f", @{row['username']}" if row.get("username") else "") + ")") if row else "Telegram request: none"
    await update.effective_message.reply_text(f"Trader ID {tid}\n{req}\n{describe_summary(s, await st.trader_summary(tid))}")


async def signal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/signal <text>: preview first, nothing is sent until the admin presses Send."""
    if not _is_admin(update, context):
        return
    text = " ".join(context.args).strip() if context.args else ""
    if not text:
        await update.effective_message.reply_text("Usage: /signal <your message>\nExample: /signal EURUSD CALL 5 min")
        return
    if len(text) > MAX_SIGNAL_CHARS:
        await update.effective_message.reply_text(f"Too long (max {MAX_SIGNAL_CHARS} characters).")
        return
    n = len(await _store(context).list_approved_ids())
    context.application.bot_data.setdefault("signal_drafts", {})[update.effective_user.id] = text + SIGNAL_FOOTER
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"Send to {n} approved users", callback_data="sg:send"),
                                InlineKeyboardButton("Cancel", callback_data="sg:cancel")]])
    await update.effective_message.reply_text(f"Preview:\n\n{text}{SIGNAL_FOOTER}", reply_markup=kb)


async def on_signal_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    s = _s(context)
    if not s.is_admin(q.from_user.id, q.message.chat.id if q.message else None):
        await q.answer("Only admins can do this.", show_alert=True)
        return
    draft = context.application.bot_data.setdefault("signal_drafts", {}).pop(q.from_user.id, None)
    if q.data == "sg:cancel" or draft is None:
        await q.answer("Cancelled." if draft is not None else "Nothing to send (expired). Use /signal again.")
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass
        return
    await q.answer("Sending...")
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    ids = await _store(context).list_approved_ids()
    sent, failed = await broadcast(context.bot, ids, draft)
    await context.bot.send_message(q.message.chat.id, f"Signal sent to {sent} users. Failed/blocked: {failed}.")


async def analysis_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/analysis EURUSD [1m|5m|15m|1h|1d] - admin only, shown to the admin only, never broadcast."""
    if not _is_admin(update, context):
        return
    msg = update.effective_message
    args = context.args or []
    symbol = yahoo_symbol(args[0]) if args else None
    interval = (args[1].lower() if len(args) > 1 else "5m")
    if symbol is None or interval not in _INTERVALS:
        await msg.reply_text("Usage: /analysis EURUSD [1m|5m|15m|1h|1d]\nExamples: /analysis GBPUSD 15m   /analysis BTCUSD 1h")
        return
    try:
        closes = await fetch_closes(symbol, interval)
    except Exception as exc:  # noqa: BLE001 - network/Yahoo problem; type only
        logger.warning("analysis fetch failed: %s", type(exc).__name__)
        await msg.reply_text("Could not fetch market data right now. Check the symbol or try again later.")
        return
    await msg.reply_text(analysis_text(symbol, interval, closes))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Type only: network error text can contain the request URL, which holds the bot token.
    logger.error("handler error: %s", type(context.error).__name__)


def build_application(settings: Settings, polling: bool) -> Application:
    builder = Application.builder().token(settings.token)
    if not polling:
        builder = builder.updater(None)  # webhook mode: we feed updates in ourselves (see TelegramHandler)
    app = builder.build()
    app.bot_data["settings"] = settings
    app.bot_data["store"] = None  # set in amain() once the database is ready

    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("verify", verify_cmd, filters=private))
    app.add_handler(CommandHandler("status", status_cmd, filters=private))
    app.add_handler(CommandHandler("pending", pending_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("events", events_cmd))
    app.add_handler(CommandHandler("signal", signal_cmd))
    app.add_handler(CommandHandler("trader", trader_cmd))
    app.add_handler(CommandHandler("analysis", analysis_cmd))
    app.add_handler(CallbackQueryHandler(on_signal_button, pattern=r"^sg:(send|cancel)$"))
    app.add_handler(CallbackQueryHandler(on_submit_button, pattern=r"^submit$"))
    app.add_handler(CallbackQueryHandler(on_decision, pattern=r"^(ok|no):.{1,60}$"))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    return app


# ==========================================================================
# HTTP SERVER (tornado, comes with python-telegram-bot[webhooks])
# ==========================================================================
class _Base(tornado.web.RequestHandler):
    def initialize(self, ctx) -> None:  # noqa: D401
        self.ctx = ctx


class RootHandler(_Base):
    def get(self) -> None:
        self.write({"status": "running", "message": "HONESTY AI VIP BOT is online",
                    "health": "/health", "postback": "/postback"})

    def head(self) -> None:
        self.set_status(200)


class HealthHandler(_Base):
    def get(self) -> None:
        self.write({"status": "ok"})

    def head(self) -> None:
        self.set_status(200)


class PostbackHandler(_Base):
    async def _handle(self, key: str = "") -> None:
        ctx = self.ctx
        query = {k: v[-1].decode("utf-8", "replace") for k, v in self.request.query_arguments.items()}

        async def notify(text: str) -> None:
            await notify_admins(ctx.ptb.bot, ctx.settings, text)

        async def after(ev: dict) -> None:  # a new deposit may complete a pending request (AUTO_APPROVE only)
            store = ctx.ptb.bot_data.get("store")
            if store is None or not ev.get("trader_id") or ev.get("kind") not in ("ftd", "dep"):
                return
            row = await try_auto_approve(store, ctx.settings, ev["trader_id"])
            if row:
                try:
                    await ctx.ptb.bot.send_message(row["telegram_id"], approval_text(ctx.settings, row))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("could not message auto-approved user: %s", type(exc).__name__)
                await notify_admins(ctx.ptb.bot, ctx.settings, f"Auto-approved (postback deposits reach the minimum)\n{_admin_text(row)}")

        try:
            code, payload = await process_postback(
                ctx.settings, ctx.ptb.bot_data.get("store"), notify,
                dict(self.request.headers), query, self.request.body, path_key=key, after=after)
        except Exception as exc:  # noqa: BLE001 - never leak details; the partner just needs a status
            logger.error("postback error: %s", type(exc).__name__)
            code, payload = 500, {"status": "error"}
        self.set_status(code)
        self.write(payload)

    async def get(self, key: str = "") -> None:
        await self._handle(key)

    async def post(self, key: str = "") -> None:
        await self._handle(key)


class TelegramHandler(_Base):
    async def post(self) -> None:
        got = self.request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(got.encode(), self.ctx.settings.webhook_secret.encode()):
            self.set_status(403)
            return
        try:
            update = Update.de_json(json.loads(self.request.body), self.ctx.ptb.bot)
            await self.ctx.ptb.update_queue.put(update)
        except Exception as exc:  # noqa: BLE001
            logger.warning("bad telegram update: %s", type(exc).__name__)
            self.set_status(400)
            return
        self.set_status(200)


def make_http_app(ctx) -> tornado.web.Application:
    routes = [
        (r"/", RootHandler, dict(ctx=ctx)),
        (r"/health", HealthHandler, dict(ctx=ctx)),
        (r"/postback", PostbackHandler, dict(ctx=ctx)),
        (r"/([A-Za-z0-9_\-]{1,128})/postback", PostbackHandler, dict(ctx=ctx)),  # partner format: /<key>/postback
    ]
    if ctx.settings.use_webhook:
        routes.append((r"/" + re.escape(ctx.settings.webhook_path), TelegramHandler, dict(ctx=ctx)))
    return tornado.web.Application(routes)


# ==========================================================================
# ENTRY POINT
# ==========================================================================
async def amain() -> None:
    settings = load_settings()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # These log full request URLs (bot token / postback key / partner parameters): keep them quiet.
    for noisy in ("httpx", "httpcore", "tornado.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    polling = not settings.use_webhook
    ptb = build_application(settings, polling)
    ctx = SimpleNamespace(settings=settings, ptb=ptb)

    # 1) open the port first so Render sees the service as up while the database connects
    server = make_http_app(ctx).listen(settings.port, address="0.0.0.0", max_body_size=MAX_BODY * 2)
    logger.info("HTTP server listening on port %s (%s mode)", settings.port, "polling" if polling else "webhook")

    # 2) storage; a database problem must never stop the deploy
    store = PostgresStore(settings.database_url) if settings.database_url else MemoryStore()
    try:
        await store.init()
    except Exception as exc:  # noqa: BLE001
        logger.warning("DATABASE UNAVAILABLE (%s): using in-memory storage, data is lost on restart. "
                       "Check DATABASE_URL.", type(exc).__name__)
        store = MemoryStore()
    if isinstance(store, MemoryStore):
        logger.warning("Running with in-memory storage (no persistent database)")
    ptb.bot_data["store"] = store

    # 3) Telegram
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # e.g. Windows
            pass

    async with ptb:
        await ptb.start()
        if polling:
            await ptb.updater.start_polling(drop_pending_updates=True)
        else:
            url = f"{settings.webhook_base_url}/{settings.webhook_path}"
            for attempt in range(1, 4):
                try:
                    await ptb.bot.set_webhook(url, secret_token=settings.webhook_secret,
                                              allowed_updates=["message", "callback_query"])
                    logger.info("Telegram webhook registered")
                    break
                except Exception as exc:  # noqa: BLE001
                    logger.error("set_webhook failed (attempt %s/3): %s", attempt, type(exc).__name__)
                    await asyncio.sleep(2 * attempt)
        await stop.wait()
        logger.info("shutting down")
        if polling:
            await ptb.updater.stop()
        await ptb.stop()
    server.stop()
    await store.close()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
