#!/usr/bin/env python3
"""
pingx — full-screen TUI ping monitor with auto-reconnect and WAN failover detection.
Requires: pip install rich
"""

import argparse
import os
import platform
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

__version__ = "1.0.0"

try:
    from rich.layout   import Layout
    from rich.live     import Live
    from rich.panel    import Panel
    from rich.text     import Text
    from rich.table    import Table
    from rich.align    import Align
    from rich.console  import Console
    from rich.style    import Style
    from rich          import box as rbox
except ImportError:
    sys.exit("pingx requires rich:  pip install rich")


# ── Platform check ────────────────────────────────────────────────────────────

def check_platform() -> None:
    """Exit with a helpful message on unsupported platforms."""
    system = platform.system()
    if system == "Windows":
        sys.exit(
            "pingx does not support Windows.\n"
            "Use WSL2 (Windows Subsystem for Linux) to run pingx."
        )
    if system == "Linux":
        # Test whether unprivileged ICMP is available
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
            s.close()
        except PermissionError:
            sys.exit(
                "pingx: unprivileged ICMP is not enabled on this system.\n\n"
                "To fix, run once as root:\n"
                "  sudo sysctl -w net.ipv4.ping_group_range=\"0 65535\"\n\n"
                "To make it permanent, add to /etc/sysctl.conf:\n"
                "  net.ipv4.ping_group_range = 0 65535\n\n"
                "Or run pingx with sudo."
            )


# ── Colour themes ─────────────────────────────────────────────────────────────

@dataclass
class ColorTheme:
    name: str
    # Logo gradient (6 lines, top→bottom)
    logo_colors: list
    # Panel borders
    border_ok: str
    border_down: str
    # Status indicator
    connected_color: str
    down_color: str
    # Route / info accents
    accent: str
    # Sparkline health bands (wide, for stability)
    spark_ok: str       # < 80ms
    spark_warn: str     # < 150ms
    spark_alert: str    # < 300ms
    spark_crit: str     # > 300ms
    # Stats text fine-grained bands
    rtt_fast: str       # < 20ms
    rtt_good: str       # < 50ms
    rtt_fair: str       # < 100ms
    rtt_slow: str       # < 200ms
    rtt_crit: str       # >= 200ms
    # Loss colour bands
    loss_ok: str        # 0%
    loss_low: str       # < 2%
    loss_mid: str       # < 5%
    loss_high: str      # < 15%
    # Misc
    dim_color: str      # y-axis labels, separators
    pad_color: str      # padding dots (before data)


THEMES: dict[str, ColorTheme] = {
    "green": ColorTheme(
        name="green",
        logo_colors=["bright_white", "bright_green", "bright_green", "green", "green", "dark_green"],
        border_ok="green", border_down="red",
        connected_color="bright_green", down_color="red",
        accent="cyan",
        spark_ok="bright_green", spark_warn="yellow", spark_alert="orange1", spark_crit="red",
        rtt_fast="bright_green", rtt_good="green", rtt_fair="yellow", rtt_slow="orange1", rtt_crit="red",
        loss_ok="bright_green", loss_low="green", loss_mid="yellow", loss_high="orange1",
        dim_color="grey42", pad_color="red",
    ),
    "blue": ColorTheme(
        name="blue",
        logo_colors=["bright_white", "bright_blue", "bright_blue", "blue", "blue", "navy_blue"],
        border_ok="bright_blue", border_down="red",
        connected_color="bright_blue", down_color="red",
        accent="cyan",
        spark_ok="bright_blue", spark_warn="blue", spark_alert="orange1", spark_crit="red",
        rtt_fast="bright_blue", rtt_good="blue", rtt_fair="cyan", rtt_slow="orange1", rtt_crit="red",
        loss_ok="bright_blue", loss_low="blue", loss_mid="cyan", loss_high="orange1",
        dim_color="grey42", pad_color="red",
    ),
    "cyan": ColorTheme(
        name="cyan",
        logo_colors=["bright_white", "bright_cyan", "bright_cyan", "cyan", "cyan", "dark_cyan"],
        border_ok="bright_cyan", border_down="red",
        connected_color="bright_cyan", down_color="red",
        accent="bright_cyan",
        spark_ok="bright_cyan", spark_warn="cyan", spark_alert="orange1", spark_crit="red",
        rtt_fast="bright_cyan", rtt_good="cyan", rtt_fair="yellow", rtt_slow="orange1", rtt_crit="red",
        loss_ok="bright_cyan", loss_low="cyan", loss_mid="yellow", loss_high="orange1",
        dim_color="grey42", pad_color="red",
    ),
    "amber": ColorTheme(
        name="amber",
        logo_colors=["bright_white", "bright_yellow", "bright_yellow", "yellow", "yellow", "dark_goldenrod"],
        border_ok="yellow", border_down="red",
        connected_color="bright_yellow", down_color="red",
        accent="bright_yellow",
        spark_ok="bright_yellow", spark_warn="yellow", spark_alert="orange1", spark_crit="red",
        rtt_fast="bright_yellow", rtt_good="yellow", rtt_fair="orange1", rtt_slow="red", rtt_crit="bold red",
        loss_ok="bright_yellow", loss_low="yellow", loss_mid="orange1", loss_high="red",
        dim_color="grey42", pad_color="dark_red",
    ),
    "red": ColorTheme(
        name="red",
        logo_colors=["bright_white", "bright_red", "bright_red", "red", "red", "dark_red"],
        border_ok="red", border_down="bright_red",
        connected_color="bright_red", down_color="bold bright_red",
        accent="bright_red",
        spark_ok="bright_red", spark_warn="red", spark_alert="orange1", spark_crit="bright_white",
        rtt_fast="bright_red", rtt_good="red", rtt_fair="orange1", rtt_slow="yellow", rtt_crit="bright_white",
        loss_ok="bright_red", loss_low="red", loss_mid="orange1", loss_high="yellow",
        dim_color="grey42", pad_color="dark_red",
    ),
    "purple": ColorTheme(
        name="purple",
        logo_colors=["bright_white", "bright_magenta", "bright_magenta", "magenta", "purple4", "purple4"],
        border_ok="bright_magenta", border_down="red",
        connected_color="bright_magenta", down_color="red",
        accent="bright_magenta",
        spark_ok="bright_magenta", spark_warn="magenta", spark_alert="orange1", spark_crit="red",
        rtt_fast="bright_magenta", rtt_good="magenta", rtt_fair="yellow", rtt_slow="orange1", rtt_crit="red",
        loss_ok="bright_magenta", loss_low="magenta", loss_mid="yellow", loss_high="orange1",
        dim_color="grey42", pad_color="red",
    ),
}

THEME_NAMES = list(THEMES.keys())

# Active theme (set in main() after arg parse)
_theme: ColorTheme = THEMES["green"]


# ── Shared state ──────────────────────────────────────────────────────────────

@dataclass
class PingState:
    lock:          threading.Lock          = field(default_factory=threading.Lock)
    ticker:        deque                   = field(default_factory=lambda: deque(maxlen=5000))
    events:        deque                   = field(default_factory=lambda: deque(maxlen=20000))
    total_sent:    int                     = 0
    total_recv:    int                     = 0
    rtts:          deque                   = field(default_factory=lambda: deque(maxlen=10000))
    current_rtt:   Optional[float]         = None
    net_down:      bool                    = False
    down_start:    float                   = 0.0
    down_attempts: int                     = 0
    route:         Optional[str]           = None
    failovers:     deque                   = field(default_factory=lambda: deque(maxlen=2))
    start_mono:    float                   = field(default_factory=time.monotonic)
    running:       bool                    = True
    target:        str                     = ""
    target_ip:     str                     = ""
    # CLI-configured params
    interval:      float                   = 0.2
    timeout:       float                   = 1.5
    count:         int                     = 0    # 0 = unlimited
    ttl:           int                     = 64
    pkt_size:      int                     = 56   # ICMP payload bytes


_state = PingState()


# ── ICMP helpers ──────────────────────────────────────────────────────────────

def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b'\x00'
    s = sum((data[i] << 8) + data[i + 1] for i in range(0, len(data), 2))
    s = (s >> 16) + (s & 0xffff)
    return (~(s + (s >> 16))) & 0xffff


def _build_echo(seq: int) -> bytes:
    ident   = os.getpid() & 0xffff
    payload = struct.pack('!d', time.monotonic()) + b'\x00' * max(0, _state.pkt_size - 8)
    hdr     = struct.pack('!BBHHH', 8, 0, 0, ident, seq & 0xffff)
    chk     = _checksum(hdr + payload)
    return struct.pack('!BBHHH', 8, 0, chk, ident, seq & 0xffff) + payload


# ── Window stats (call inside lock) ──────────────────────────────────────────

def _window_loss(window_secs: float, st: PingState):
    cutoff = time.monotonic() - window_secs
    sent = recv = 0
    for ts, received in reversed(st.events):
        if ts < cutoff:
            break
        sent += 1
        if received:
            recv += 1
    loss = (sent - recv) / sent * 100 if sent else 0.0
    return loss, sent, recv


# ── Route monitor ─────────────────────────────────────────────────────────────

def _get_route() -> Optional[str]:
    try:
        out = subprocess.run(
            ['route', 'get', 'default'],
            capture_output=True, text=True, timeout=2
        ).stdout
        for line in out.splitlines():
            s = line.strip()
            if s.startswith('gateway:'):
                return s.split(':', 1)[1].strip()
    except Exception:
        pass
    return None


def _route_monitor(st: PingState) -> None:
    prev = _get_route()
    with st.lock:
        st.route = prev
    while st.running:
        time.sleep(3)
        new = _get_route()
        if new:
            with st.lock:
                st.route = new
            if prev and new != prev:
                with st.lock:
                    st.failovers.append({
                        'time': datetime.now().strftime('%H:%M:%S'),
                        'type': 'route',
                        'from': prev,
                        'to':   new,
                    })
            prev = new


# ── Ping loop ─────────────────────────────────────────────────────────────────

DOWN_THRESHOLD = 5


def _ping_loop(st: PingState) -> None:
    seq   = 0
    cfail = 0

    while st.running:
        if st.count > 0 and seq >= st.count:
            st.running = False
            break

        t0   = time.monotonic()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
        sock.settimeout(st.timeout)

        try:
            if st.ttl != 64:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, st.ttl)

            send_ts = time.monotonic()
            sock.sendto(_build_echo(seq), (st.target_ip, 0))

            with st.lock:
                st.events.append((send_ts, False))
                # total_sent is NOT incremented here — wait until outcome is
                # known so total_sent and total_recv always update atomically.
                # Incrementing sent before recv caused lost to transiently
                # read 1 during every successful ping's receive window.

            _data, _addr = sock.recvfrom(256)
            rtt = (time.monotonic() - send_ts) * 1000

            with st.lock:
                if st.events:
                    st.events[-1] = (st.events[-1][0], True)
                st.total_sent  += 1   # ← both updated atomically on success
                st.total_recv  += 1
                st.current_rtt  = rtt
                st.rtts.append(rtt)
                st.ticker.append({'received': True, 'rtt': rtt})

            cfail = 0

            if st.net_down:
                st.net_down  = False
                down_secs    = time.monotonic() - st.down_start
                with st.lock:
                    for evt in reversed(st.failovers):
                        if evt['type'] == 'down' and 'recovered' not in evt:
                            evt['recovered'] = datetime.now().strftime('%H:%M:%S')
                            evt['down_secs'] = down_secs
                            break

        except (socket.timeout, OSError):
            cfail += 1
            with st.lock:
                st.total_sent  += 1   # ← outcome known: timed out / error
                st.ticker.append({'received': False, 'rtt': None})
                st.current_rtt = None

            if cfail == DOWN_THRESHOLD and not st.net_down:
                st.net_down      = True
                st.down_start    = time.monotonic()
                st.down_attempts = 0
                with st.lock:
                    st.failovers.append({
                        'time': datetime.now().strftime('%H:%M:%S'),
                        'type': 'down',
                    })

            if st.net_down:
                st.down_attempts += 1

        finally:
            sock.close()

        seq += 1
        time.sleep(max(0.0, st.interval - (time.monotonic() - t0)))


# ── Colour helpers ────────────────────────────────────────────────────────────

def _rtt_style(rtt: Optional[float]) -> Style:
    """Fine-grained style for stats panel text."""
    th = _theme
    if rtt is None:    return Style(color="grey23")
    if rtt <  20:      return Style(color=th.rtt_fast, bold=True)
    if rtt <  50:      return Style(color=th.rtt_good)
    if rtt < 100:      return Style(color=th.rtt_fair)
    if rtt < 200:      return Style(color=th.rtt_slow)
    return                    Style(color=th.rtt_crit, bold=True)


def _sparkline_color(rtt: Optional[float]) -> str:
    """Wide health-band colours for the sparkline.
    Wide bands prevent colour flipping on minor RTT variation."""
    th = _theme
    if rtt is None:    return "grey23"
    if rtt <  80:      return th.spark_ok
    if rtt < 150:      return th.spark_warn
    if rtt < 300:      return th.spark_alert
    return                    th.spark_crit


def _rtt_markup(val: Optional[float], suffix: str = " ms") -> str:
    if val is None:
        return "[dim]—[/]"
    s = _rtt_style(val)
    c = s.color.name if s.color else "white"
    bold = "bold " if s.bold else ""
    return f"[{bold}{c}]{val:.2f}{suffix}[/]"


def _loss_markup(pct: float) -> str:
    th = _theme
    if pct == 0:   return f"[{th.loss_ok}]0.0%[/]"
    if pct <  2:   return f"[{th.loss_low}]{pct:.1f}%[/]"
    if pct <  5:   return f"[{th.loss_mid}]{pct:.1f}%[/]"
    if pct < 15:   return f"[{th.loss_high}]{pct:.1f}%[/]"
    return              f"[bold red]{pct:.1f}%[/]"


# ── ASCII art logo ────────────────────────────────────────────────────────────

_LOGO_LINES = [
    "██████╗ ██╗███╗  ██╗ ██████╗ ██╗  ██╗",
    "██╔══██╗██║████╗ ██║██╔════╝ ╚██╗██╔╝",
    "██████╔╝██║██╔██╗██║██║  ███╗  ╚███╔╝ ",
    "██╔═══╝ ██║██║╚████║██║   ██║  ██╔██╗ ",
    "██║     ██║██║ ╚███║╚██████╔╝ ██╔╝╚██╗",
    "╚═╝     ╚═╝╚═╝  ╚══╝ ╚═════╝ ╚═╝  ╚═╝",
]


# ── Panel builders ────────────────────────────────────────────────────────────

def _panel(content, title: str, down: bool) -> Panel:
    th     = _theme
    border = th.border_down if down else th.border_ok
    return Panel(content, title=title, border_style=border,
                 box=rbox.HEAVY, padding=(0, 1))


def build_logo(st: PingState) -> Panel:
    with st.lock:
        down     = st.net_down
        attempts = st.down_attempts
        route    = st.route
        target   = st.target
        tip      = st.target_ip

    elapsed = time.monotonic() - st.start_mono
    h = int(elapsed // 3600)
    m = int((elapsed % 3600) // 60)
    s = int(elapsed % 60)

    th = _theme
    t  = Text()
    t.append("\n")
    for line, color in zip(_LOGO_LINES, th.logo_colors):
        bold = color in ("bright_white", th.logo_colors[1])
        t.append(f"  {line}\n", style=Style(color=color, bold=bold))

    t.append("\n")

    if down:
        t.append("  ● ", style=Style(color=th.down_color, bold=True))
        t.append("NETWORK DOWN", style=Style(color=th.down_color, bold=True))
        t.append(f"  attempt {attempts}\n", style=Style(color=th.down_color, dim=True))
    else:
        t.append("  ● ", style=Style(color=th.connected_color, bold=True))
        t.append("CONNECTED\n", style=Style(color=th.connected_color, bold=True))

    t.append("\n")
    t.append("  host   ", style=Style(dim=True))
    t.append(f"{target}", style=Style(color="white", bold=True))
    t.append(f"  ({tip})\n", style=Style(dim=True))

    t.append("  route  ", style=Style(dim=True))
    t.append(f"{route or '—'}\n", style=Style(color=th.accent))

    t.append("  uptime ", style=Style(dim=True))
    t.append(f"{h:02d}:{m:02d}:{s:02d}\n", style=Style(dim=True))

    title_color = th.border_ok
    return _panel(Align(t, "left", vertical="middle"),
                  f"[bold {title_color}] P I N G X [/]", down)


def build_stats(st: PingState) -> Panel:
    with st.lock:
        ts   = st.total_sent
        tr   = st.total_recv
        cur  = st.current_rtt
        rtts = list(st.rtts)
        down = st.net_down
        l5,  s5,  _ = _window_loss(300,  st)
        l1h, s1h, _ = _window_loss(3600, st)

    loss_total = (ts - tr) / ts * 100 if ts else 0.0
    mn  = min(rtts) if rtts else None
    mx  = max(rtts) if rtts else None
    avg = sum(rtts) / len(rtts) if rtts else None

    tbl = Table(show_header=False, box=None, padding=(0, 2), expand=False)
    tbl.add_column(style="dim", justify="right", min_width=13)
    tbl.add_column(justify="left", min_width=16)

    tbl.add_row()
    tbl.add_row("[dim]current rtt[/]", Text.from_markup(_rtt_markup(cur)))
    tbl.add_row("[dim]average rtt[/]", Text.from_markup(_rtt_markup(avg)))
    tbl.add_row("[dim]min rtt[/]",     Text.from_markup(_rtt_markup(mn)))
    tbl.add_row("[dim]max rtt[/]",     Text.from_markup(_rtt_markup(mx)))
    tbl.add_row()
    tbl.add_row("[dim]5-min loss[/]",
                Text.from_markup(f"{_loss_markup(l5)}  [dim]({s5:,} pkts)[/]"))
    tbl.add_row("[dim]1-hr  loss[/]",
                Text.from_markup(f"{_loss_markup(l1h)}  [dim]({s1h:,} pkts)[/]"))
    tbl.add_row("[dim]total loss[/]",
                Text.from_markup(_loss_markup(loss_total)))
    tbl.add_row()
    tbl.add_row("[dim]sent[/]",     Text.from_markup(f"[dim]{ts:,}[/]"))
    tbl.add_row("[dim]received[/]", Text.from_markup(f"[dim]{tr:,}[/]"))
    tbl.add_row("[dim]lost[/]",     Text.from_markup(f"[dim]{ts - tr:,}[/]"))
    tbl.add_row()

    return _panel(Align(tbl, "center", vertical="middle"),
                  "[bold] STATISTICS [/]", down)


def build_visualizer(panel_w: int, panel_h: int, st: PingState) -> Panel:
    with st.lock:
        ticker = list(st.ticker)
        down   = st.net_down

    # Each braille char = 2 data columns wide, 4 pixel rows tall
    chart_rows = max(3, panel_h - 6)
    chart_cols = max(4, panel_w - 12)
    n_steps    = chart_cols * 2
    total_h    = chart_rows * 4

    # Align ticker to data window. actual[i]=None means padding (no ping yet).
    recent_entries = ticker[-n_steps:] if len(ticker) >= n_steps else ticker
    actual = [None] * (n_steps - len(recent_entries)) + list(recent_entries)
    data   = [e['rtt'] if e and e['received'] else None for e in actual]

    # Auto-scale Y-axis to observed range
    valid = [v for v in data if v is not None]
    if len(valid) >= 2:
        rtt_lo = min(valid)
        rtt_hi = max(valid)
        pad    = max(1.0, (rtt_hi - rtt_lo) * 0.12)
        rtt_lo = max(0.0, rtt_lo - pad)
        rtt_hi = rtt_hi + pad
    elif valid:
        rtt_lo = max(0.0, valid[0] - 5.0)
        rtt_hi = valid[0] + 5.0
    else:
        rtt_lo, rtt_hi = 0.0, 100.0

    if rtt_hi - rtt_lo < 1.0:
        rtt_hi = rtt_lo + 1.0

    def to_px(rtt: float) -> int:
        norm = (rtt - rtt_lo) / (rtt_hi - rtt_lo)
        return max(1, min(total_h, round(norm * (total_h - 1)) + 1))

    px_heights = [to_px(v) if v is not None else 0 for v in data]

    ylw = max(len(f"{rtt_hi:.0f}"), len(f"{rtt_lo:.0f}"), 3) + 2

    th = _theme
    t  = Text()

    for row_idx in range(chart_rows):
        row_px_top = row_idx * 4

        # Y-axis labels
        mid_row = chart_rows // 2
        if row_idx == 0:
            t.append(f"{rtt_hi:.0f}ms".rjust(ylw), style=Style(dim=True))
            t.append(" ┤", style=Style(color=th.dim_color, dim=True))
        elif row_idx == mid_row:
            mid_rtt = (rtt_hi + rtt_lo) / 2
            t.append(f"{mid_rtt:.0f}ms".rjust(ylw), style=Style(dim=True))
            t.append(" ┼", style=Style(color=th.dim_color, dim=True))
        elif row_idx == chart_rows - 1:
            t.append(f"{rtt_lo:.0f}ms".rjust(ylw), style=Style(dim=True))
            t.append(" ┤", style=Style(color=th.dim_color, dim=True))
        else:
            t.append(" " * ylw + "  ", style=Style(dim=True))

        # Braille columns
        for col_idx in range(chart_cols):
            li = col_idx * 2
            ri = col_idx * 2 + 1

            is_last  = (col_idx == chart_cols - 1)
            l_entry  = actual[li]
            r_entry  = actual[ri]

            l_has_data = l_entry is not None
            r_has_data = r_entry is not None

            # True padding — no pings received yet
            if not l_has_data and not r_has_data:
                t.append("⣀", style=Style(color=th.pad_color, dim=True))
                continue

            l_timeout = l_has_data and not l_entry['received']
            r_timeout = r_has_data and not r_entry['received']

            lh = px_heights[li]
            rh = px_heights[ri]

            # Build braille char from filled-from-bottom bars
            # Bit layout: left col bits 0,1,2,6 (rows 0-3); right col bits 3,4,5,7
            char_val = 0
            for bit, bp in ((0, 0), (1, 1), (2, 2), (6, 3)):
                p = row_px_top + bp
                if not l_timeout and lh > 0 and p >= (total_h - lh):
                    char_val |= (1 << bit)
            for bit, bp in ((3, 0), (4, 1), (5, 2), (7, 3)):
                p = row_px_top + bp
                if not r_timeout and rh > 0 and p >= (total_h - rh):
                    char_val |= (1 << bit)

            ch    = chr(0x2800 + char_val)
            color = _sparkline_color(data[ri] if not r_timeout else data[li])
            style = Style(color=color, bold=True) if is_last else Style(color=color)
            t.append(ch, style=style)

        t.append("\n")

    axis = " " * (ylw + 2) + "└" + "─" * max(1, chart_cols - 6) + " now →"
    t.append(axis + "\n", style=Style(color=th.dim_color, dim=True))

    t.append("\n")
    for char, color, label in (
        ("⣿", th.spark_ok,    "< 80ms "),
        ("⣿", th.spark_warn,  "< 150ms"),
        ("⣿", th.spark_alert, "< 300ms"),
        ("⣿", th.spark_crit,  "> 300ms"),
        ("⣀", th.pad_color,   "timeout"),
    ):
        t.append(f"  {char}", style=Style(color=color))
        t.append(f" {label}", style=Style(dim=True))

    return _panel(t, "[bold] LATENCY HISTORY [/]", down)


def build_events(st: PingState) -> Panel:
    with st.lock:
        down      = st.net_down
        attempts  = st.down_attempts
        route     = st.route
        failovers = list(st.failovers)

    th = _theme
    t  = Text()
    t.append("\n")

    t.append("  CURRENT ROUTE\n", style=Style(dim=True, bold=True))
    t.append("  " + "─" * 22 + "\n", style=Style(dim=True))

    if route:
        t.append(f"  {route}\n", style=Style(color=th.accent, bold=True))
    else:
        t.append("  —\n", style=Style(dim=True))

    if down:
        t.append(f"\n  ● NETWORK DOWN\n", style=Style(color=th.down_color, bold=True))
        t.append(f"  retrying... attempt {attempts}\n",
                 style=Style(color=th.down_color, dim=True))

    t.append("\n")
    t.append("  FAILOVER HISTORY\n", style=Style(dim=True, bold=True))
    t.append("  " + "─" * 22 + "\n", style=Style(dim=True))

    if not failovers:
        t.append("  no events yet\n", style=Style(dim=True))
    else:
        for evt in reversed(failovers):
            etype = evt.get('type')
            ts    = evt.get('time', '?')
            t.append(f"  {ts}  ", style=Style(dim=True))

            if etype == 'down':
                t.append("▼ NETWORK DOWN", style=Style(color=th.down_color, bold=True))
                if 'recovered' in evt:
                    secs = evt.get('down_secs', 0)
                    t.append(f"\n         ↑ recovered {evt['recovered']}",
                             style=Style(color=th.connected_color, dim=True))
                    t.append(f"  ({secs:.1f}s)\n", style=Style(dim=True))
                else:
                    t.append("\n")

            elif etype == 'route':
                frm = evt.get('from', '?')
                to  = evt.get('to',   '?')
                t.append("⇄ WAN FAILOVER\n", style=Style(color="yellow", bold=True))
                t.append(f"         {frm}\n", style=Style(dim=True))
                t.append(f"         → {to}\n", style=Style(color=th.accent))

            t.append("\n")

    return _panel(Align(t, "left"), "[bold] WAN & EVENTS [/]", down)


# ── Layout ────────────────────────────────────────────────────────────────────

def _make_layout() -> Layout:
    root = Layout()
    root.split_column(
        Layout(name="top",    ratio=2),
        Layout(name="bottom", ratio=3),
    )
    root["top"].split_row(
        Layout(name="logo",   ratio=5),
        Layout(name="stats",  ratio=4),
    )
    root["bottom"].split_row(
        Layout(name="viz",    ratio=3),
        Layout(name="events", ratio=2),
    )
    return root


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pingx",
        description="Full-screen TUI ping monitor with auto-reconnect and WAN failover detection.",
    )
    parser.add_argument("host", help="Hostname or IP address to ping")
    parser.add_argument("-c", "--count",    type=int,   default=0,
                        metavar="N",
                        help="Stop after N pings (default: unlimited)")
    parser.add_argument("-i", "--interval", type=float, default=0.2,
                        metavar="SECS",
                        help="Ping interval in seconds (default: 0.2)")
    parser.add_argument("-s", "--size",     type=int,   default=56,
                        metavar="BYTES",
                        help="ICMP payload size in bytes (default: 56)")
    parser.add_argument("-t", "--ttl",      type=int,   default=64,
                        metavar="TTL",
                        help="IP Time To Live (default: 64)")
    parser.add_argument("-W", "--timeout",  type=float, default=1.5,
                        metavar="SECS",
                        help="Receive timeout per ping in seconds (default: 1.5)")
    parser.add_argument("--color",          default="green",
                        choices=THEME_NAMES,
                        metavar="THEME",
                        help=f"Colour theme: {', '.join(THEME_NAMES)} (default: green)")
    parser.add_argument("--version",        action="version",
                        version=f"pingx {__version__}")
    return parser.parse_args()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    global _theme

    args = _parse_args()
    check_platform()

    _theme = THEMES[args.color]

    st = _state
    st.target   = args.host
    st.interval = max(0.05, args.interval)
    st.timeout  = max(0.1,  args.timeout)
    st.count    = max(0,    args.count)
    st.ttl      = max(1,    args.ttl)
    st.pkt_size = max(8,    args.size)

    try:
        st.target_ip = socket.gethostbyname(args.host)
    except socket.gaierror as e:
        sys.exit(f"pingx: cannot resolve '{args.host}': {e}")

    console = Console()

    def _on_exit(sig, frame):
        st.running = False

    signal.signal(signal.SIGINT,  _on_exit)
    signal.signal(signal.SIGTERM, _on_exit)

    threading.Thread(target=_route_monitor, args=(st,), daemon=True).start()
    threading.Thread(target=_ping_loop,     args=(st,), daemon=True).start()

    layout = _make_layout()

    with Live(layout, console=console, screen=True,
              refresh_per_second=10) as _live:
        while st.running:
            w = console.width
            h = console.height

            viz_w = max(10, int(w * 3 / 5) - 4)
            viz_h = max(5,  int(h * 3 / 5) - 2)

            layout["logo"].update(build_logo(st))
            layout["stats"].update(build_stats(st))
            layout["viz"].update(build_visualizer(viz_w, viz_h, st))
            layout["events"].update(build_events(st))

            time.sleep(0.1)

    # Post-exit summary
    with st.lock:
        ts       = st.total_sent
        tr       = st.total_recv
        rtt_list = list(st.rtts)

    loss = (ts - tr) / ts * 100 if ts else 0.0
    console.print()
    console.print(f"[bold]--- pingx {st.target} statistics ---[/]")
    console.print(
        f"{ts:,} packets transmitted, {tr:,} received, "
        f"{_loss_markup(loss)} packet loss"
    )
    if rtt_list:
        mn  = min(rtt_list)
        mx  = max(rtt_list)
        avg = sum(rtt_list) / len(rtt_list)
        std = (sum((r - avg) ** 2 for r in rtt_list) / len(rtt_list)) ** 0.5
        console.print(
            f"round-trip min/avg/max/stddev = "
            f"{mn:.3f}/{avg:.3f}/{mx:.3f}/{std:.3f} ms"
        )
    console.print()


if __name__ == '__main__':
    main()
