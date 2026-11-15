"""Chart restyle (dark, TradingView-like, like the example photo) + /admin shortcut.

Render Start Command:   python chart_style.py
It loads run.py (and main-4.py) unchanged, then only replaces the chart drawing, removes the
warning line from signal messages, and adds /admin.
"""
import struct
import zlib

import run

W, H = 1080, 540
BG, GRID, AXIS = (8, 8, 15), (21, 21, 30), (50, 50, 62)
UP, DN, DIM, WHITE = (34, 172, 98), (226, 90, 78), (132, 138, 150), (240, 242, 246)


def render_chart(candles, direction, title="", tz_offset_min=0):
    """Candlestick PNG: title top-left, price scale on the right, faint grid, last-price tag, CALL/PUT arrow."""
    cs = candles[-60:]
    n = len(cs)
    lo, hi = min(c["l"] for c in cs), max(c["h"] for c in cs)
    pad = (hi - lo) * 0.12 or abs(hi) * 1e-4 or 1e-4
    lo, hi = lo - pad, hi + pad
    left, right, top, bottom = 10, W - 112, 44, H - 12
    pw, ph = right - left, bottom - top
    dec = 3 if hi >= 20 else 5
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

    def text(x, y0, s, col, scale=2, bold=False):
        cx = x
        for ch in str(s).upper():
            g = run.GLYPHS.get(ch)
            if g:
                for r, row in enumerate(g):
                    for c in range(5):
                        if row & (1 << (4 - c)):
                            px, py = cx + c * scale, y0 + r * scale
                            rect(px, py, px + scale - 1 + (1 if bold else 0), py + scale - 1, col)
            cx += 6 * scale
        return cx

    parts = title.split()                       # "EUR/USD 1M  IST"  ->  "EUR/USD | TF: M1"
    name = parts[0] if parts else ""
    tf = parts[1] if len(parts) > 1 else ""
    tf = "M" + tf[:-1] if tf.endswith("M") and tf[:-1].isdigit() else tf
    text(16, 12, f"{name} | TF: {tf}" if tf else name, WHITE, 3, bold=True)

    for k in range(1, 8):                       # faint horizontal grid + price labels
        gy = top + ph * k // 8
        rect(left, gy, right, gy, GRID)
        text(right + 10, gy - 6, f"{hi - (hi - lo) * k / 8:.{dec}f}", DIM, 2)
    for k in range(n - 1, -1, -6):              # faint vertical grid
        gx = left + int(pw * (k + 0.5) / n)
        rect(gx, top, gx, bottom, GRID)
    rect(right, top, right, bottom, AXIS)       # right price axis line

    slot = pw / n
    half = max(2, int(slot * 0.36))
    for i, c in enumerate(cs):
        cx = left + int(slot * (i + 0.5))
        col = UP if c["c"] >= c["o"] else DN
        rect(cx, y(c["h"]), cx, y(c["l"]), col)
        y1, y2 = sorted((y(c["o"]), y(c["c"])))
        rect(cx - half, y1, cx + half, max(y2, y1 + 1), col)

    mark = UP if direction == "CALL" else DN
    ly = y(cs[-1]["c"])
    for x in range(left, right, 14):            # dashed last-price line + price tag
        rect(x, ly, x + 7, ly, mark)
    rect(right + 2, ly - 10, W - 3, ly + 10, mark)
    text(right + 8, ly - 6, f"{cs[-1]['c']:.{dec}f}", WHITE, 2)
    cx, size = left + int(slot * (n - 0.5)), 10
    if direction == "CALL":                     # green arrow under the last candle
        t0 = y(cs[-1]["l"]) + 7
        for k in range(size):
            rect(cx - k, t0 + k, cx + k, t0 + k, UP)
    else:                                       # red arrow above the last candle
        t0 = y(cs[-1]["h"]) - 7 - size
        for k in range(size):
            rect(cx - (size - 1 - k), t0 + k, cx + (size - 1 - k), t0 + k, DN)

    raw = b"".join(b"\x00" + bytes(buf[r * W * 3:(r + 1) * W * 3]) for r in range(H))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


run.render_chart = render_chart                 # run.py looks the function up by name, so this swaps it

_orig_caption = run.build_caption


def build_caption(*args, **kwargs):
    """Same signal text, without the final 'Trade responsibly...' warning line."""
    lines = _orig_caption(*args, **kwargs).split("\n")
    return "\n".join(line for line in lines if not line.startswith("⚠️ Trade responsibly"))


run.build_caption = build_caption

_prev_build = run.bot_main.build_application


def _build(settings, polling):
    app = _prev_build(settings, polling)
    app.add_handler(run.CommandHandler("admin", run.engine_cmd))   # /admin = same panel as /engine
    return app


run.bot_main.build_application = _build

if __name__ == "__main__":
    run.bot_main.main()
