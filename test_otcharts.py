"""Stage 2 - independent OTCharts test. Touches NOTHING of the bot. Uses about 3 requests of the free 1,500/week.

    pip install otcharts
    export OTCHARTS_API_KEY=otc_live_...     (Colab: os.environ["OTCHARTS_API_KEY"] = "..."  -- never share the key)
    python test_otcharts.py [SYMBOL]          (default EURUSD_otc)
"""
import os
import sys
import time

try:
    from otcharts import Client
except ImportError:
    sys.exit("Install first:  pip install otcharts")

if not os.environ.get("OTCHARTS_API_KEY"):
    sys.exit("Set OTCHARTS_API_KEY first (do not paste it in chat).")

SYMBOL = sys.argv[1] if len(sys.argv) > 1 else "EURUSD_otc"
otc = Client()


def usage(label):
    try:
        u = otc.usage()
        print(f"[usage {label}] remaining={getattr(u, 'remaining', '?')} of {getattr(u, 'quota_per', '?')}")
    except Exception as exc:  # noqa: BLE001
        print(f"[usage {label}] unavailable: {type(exc).__name__}: {exc}")


usage("before")

print("\n1) Symbols your key can read on Quotex:")
syms = list(otc.symbols("quotex"))
for s in syms:
    print("  ", s.symbol, "-", getattr(s, "name", ""))
if SYMBOL not in [s.symbol for s in syms]:
    sys.exit(f"{SYMBOL} is not in your list - pick one from above.")

for tf, label in ((60, "M1"), (300, "M5")):
    bars = otc.candles("quotex", SYMBOL, tf=tf, limit=5)
    print(f"\n2) Last {len(bars)} {label} candles of {SYMBOL} (time, open, high, low, close):")
    for b in bars:
        print("  ", b.time, b.open, b.high, b.low, b.close)
    if len(bars) < 2:
        print("   WARNING: fewer than 2 candles returned")
    # the newest bar may still be forming: the bot must only use CLOSED candles
    print(f"   newest bar time={bars[-1].time} (treat as forming; analyse bars[:-1])")

usage("after candles")

print("\n3) Live stream, 8 ticks (one connection):")
t0, n = time.time(), 0
for tick in otc.stream("quotex", SYMBOL):
    print("  ", tick.symbol, tick.price)
    n += 1
    if n >= 8 or time.time() - t0 > 60:
        break
print(f"   got {n} ticks in {time.time() - t0:.1f}s")

usage("after stream")
print("\nDONE. Paste this output (no key) in the chat.")
