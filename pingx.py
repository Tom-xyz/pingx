#!/usr/bin/env python3
"""
pingx — full-screen TUI ping monitor
Requires: pip install rich
Usage:    pingx <host>
"""

import os, signal, socket, struct, subprocess, sys, threading, time
from collections import deque
from datetime import datetime

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

# ── Config ────────────────────────────────────────────────────────────────────
INTERVAL        = 0.2   # ping interval (s)
TIMEOUT         = 1.5   # recv timeout  (s)
DOWN_THRESHOLD  = 5     # consecutive misses → NETWORK DOWN
ROUTE_CHECK_INT = 3     # route poll interval (s)
REFRESH_HZ      = 10    # TUI redraws/sec

# ── RTT colour thresholds (ms) ────────────────────────────────────────────────
def _rtt_style(rtt):
    if rtt is None:  return Style(color="grey23")
    if rtt <  20:    return Style(color="bright_green", bold=True)
    if rtt <  50:    return Style(color="green")
    if rtt < 100:    return Style(color="yellow")
    if rtt < 200:    return Style(color="orange1")
    return              Style(color="red", bold=True)

def _rtt_markup(val, suffix=" ms"):
    if val is None: return "[dim]—[/]"
    s = _rtt_style(val)
    c = s.color.name if s.color else "white"
    bold = "bold " if s.bold else ""
    return f"[{bold}{c}]{val:.2f}{suffix}[/]"

def _loss_markup(pct):
    if pct == 0:   return "[bright_green]0.0%[/]"
    if pct <  2:   return f"[green]{pct:.1f}%[/]"
    if pct <  5:   return f"[yellow]{pct:.1f}%[/]"
    if pct < 15:   return f"[orange1]{pct:.1f}%[/]"
    return              f"[bold red]{pct:.1f}%[/]"

# ── Shared state ──────────────────────────────────────────────────────────────
_lock          = threading.Lock()
_ticker        = deque(maxlen=5000)   # {'received':bool, 'rtt':float|None}
_events        = deque(maxlen=20000)  # (mono_ts:float, received:bool)
_total_sent    = 0
_total_recv    = 0
_rtts          = deque(maxlen=10000)
_current_rtt   = None                 # most recent RTT or None
_net_down      = False
_down_start    = 0.0
_down_attempts = 0
_route         = None
_failovers     = deque(maxlen=2)      # dicts, newest last
_start_mono    = time.monotonic()
_running       = True
_target        = ""
_target_ip     = ""

# ── ICMP helpers ──────────────────────────────────────────────────────────────
def _checksum(data: bytes) -> int:
    if len(data) % 2: data += b'\x00'
    s = sum((data[i] << 8) + data[i + 1] for i in range(0, len(data), 2))
    s = (s >> 16) + (s & 0xffff)
    return (~(s + (s >> 16))) & 0xffff

def _build_echo(seq: int) -> bytes:
    ident = os.getpid() & 0xffff
    hdr   = struct.pack('!BBHHH', 8, 0, 0, ident, seq & 0xffff)
    body  = struct.pack('!d', time.monotonic()) + b'\x00' * 40
    chk   = _checksum(hdr + body)
    return struct.pack('!BBHHH', 8, 0, chk, ident, seq & 0xffff) + body

# ── Window stats (call inside _lock) ─────────────────────────────────────────
def _window_loss(window_secs: float):
    cutoff = time.monotonic() - window_secs
    sent = recv = 0
    for ts, received in reversed(_events):
        if ts < cutoff: break
        sent += 1
        if received: recv += 1
    loss = (sent - recv) / sent * 100 if sent else 0.0
    return loss, sent, recv

# ── Route monitor ─────────────────────────────────────────────────────────────
def _get_route() -> "str | None":
    try:
        out = subprocess.run(['route', 'get', 'default'],
                             capture_output=True, text=True, timeout=2).stdout
        for line in out.splitlines():
            s = line.strip()
            if s.startswith('gateway:'):
                return s.split(':', 1)[1].strip()
    except Exception:
        pass
    return None

def _route_monitor():
    global _route, _running
    prev = _get_route()
    with _lock: _route = prev
    while _running:
        time.sleep(ROUTE_CHECK_INT)
        new = _get_route()
        if new:
            with _lock: _route = new
            if prev and new != prev:
                with _lock:
                    _failovers.append({
                        'time':  datetime.now().strftime('%H:%M:%S'),
                        'type':  'route',
                        'from':  prev,
                        'to':    new,
                    })
            prev = new

# ── Ping loop ─────────────────────────────────────────────────────────────────
def _ping_loop():
    global _total_sent, _total_recv, _net_down, _down_start
    global _down_attempts, _current_rtt, _running

    seq   = 0
    cfail = 0

    while _running:
        t0   = time.monotonic()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
        sock.settimeout(TIMEOUT)

        try:
            send_ts = time.monotonic()
            sock.sendto(_build_echo(seq), (_target_ip, 0))

            with _lock:
                _events.append((send_ts, False))
                _total_sent += 1

            _data, _addr = sock.recvfrom(256)
            rtt = (time.monotonic() - send_ts) * 1000

            with _lock:
                if _events: _events[-1] = (_events[-1][0], True)
                _total_recv  += 1
                _current_rtt  = rtt
                _rtts.append(rtt)
                _ticker.append({'received': True, 'rtt': rtt})

            cfail = 0

            # Recovery
            if _net_down:
                _net_down   = False
                down_secs   = time.monotonic() - _down_start
                with _lock:
                    # Tag last down-event with recovery info
                    for evt in reversed(_failovers):
                        if evt['type'] == 'down' and 'recovered' not in evt:
                            evt['recovered']  = datetime.now().strftime('%H:%M:%S')
                            evt['down_secs']  = down_secs
                            break

        except (socket.timeout, OSError):
            cfail += 1
            with _lock:
                _ticker.append({'received': False, 'rtt': None})
                _current_rtt = None

            if cfail == DOWN_THRESHOLD and not _net_down:
                _net_down      = True
                _down_start    = time.monotonic()
                _down_attempts = 0
                with _lock:
                    _failovers.append({
                        'time': datetime.now().strftime('%H:%M:%S'),
                        'type': 'down',
                    })

            if _net_down:
                _down_attempts += 1

        finally:
            sock.close()

        seq += 1
        time.sleep(max(0.0, INTERVAL - (time.monotonic() - t0)))

# ── ASCII art logo ────────────────────────────────────────────────────────────
#   Hand-crafted PINGX in box-drawing block style
_LOGO_LINES = [
    "██████╗ ██╗███╗  ██╗ ██████╗ ██╗  ██╗",
    "██╔══██╗██║████╗ ██║██╔════╝ ╚██╗██╔╝",
    "██████╔╝██║██╔██╗██║██║  ███╗  ╚███╔╝ ",
    "██╔═══╝ ██║██║╚████║██║   ██║  ██╔██╗ ",
    "██║     ██║██║ ╚███║╚██████╔╝ ██╔╝╚██╗",
    "╚═╝     ╚═╝╚═╝  ╚══╝ ╚═════╝ ╚═╝  ╚═╝",
]

# Gradient: top lines brighter, fade down
_LOGO_COLORS = [
    "bright_white",
    "bright_green",
    "bright_green",
    "green",
    "green",
    "dark_green",
]

# ── Panel builders ────────────────────────────────────────────────────────────

def _panel(content, title: str, down: bool) -> Panel:
    border = "red" if down else "green"
    return Panel(content, title=title, border_style=border,
                 box=rbox.HEAVY, padding=(0, 1))


def build_logo() -> Panel:
    with _lock:
        down     = _net_down
        attempts = _down_attempts
        route    = _route
        ts       = _total_sent
        tr       = _total_recv

    elapsed = time.monotonic() - _start_mono
    h = int(elapsed // 3600)
    m = int((elapsed % 3600) // 60)
    s = int(elapsed % 60)

    t = Text()
    t.append("\n")
    for line, color in zip(_LOGO_LINES, _LOGO_COLORS):
        t.append(f"  {line}\n", style=Style(color=color, bold=(color in ("bright_white", "bright_green"))))

    t.append("\n")

    if down:
        t.append("  ● ", style=Style(color="red", bold=True))
        t.append("NETWORK DOWN", style=Style(color="red", bold=True))
        t.append(f"  attempt {attempts}\n", style=Style(color="red", dim=True))
    else:
        t.append("  ● ", style=Style(color="bright_green", bold=True))
        t.append("CONNECTED\n", style=Style(color="bright_green", bold=True))

    t.append("\n")
    t.append("  host   ", style=Style(dim=True))
    t.append(f"{_target}", style=Style(color="white", bold=True))
    t.append(f"  ({_target_ip})\n", style=Style(dim=True))

    t.append("  route  ", style=Style(dim=True))
    t.append(f"{route or '—'}\n", style=Style(color="cyan"))

    t.append("  uptime ", style=Style(dim=True))
    t.append(f"{h:02d}:{m:02d}:{s:02d}\n", style=Style(dim=True))

    return _panel(Align(t, "left", vertical="middle"),
                  "[bold green] P I N G X [/]", down)


def build_stats() -> Panel:
    with _lock:
        ts       = _total_sent
        tr       = _total_recv
        cur      = _current_rtt
        rtts     = list(_rtts)
        down     = _net_down
        l5,  s5,  _ = _window_loss(300)
        l1h, s1h, _ = _window_loss(3600)

    loss_total = (ts - tr) / ts * 100 if ts else 0.0
    mn  = min(rtts) if rtts else None
    mx  = max(rtts) if rtts else None
    avg = sum(rtts) / len(rtts) if rtts else None

    tbl = Table(show_header=False, box=None, padding=(0, 2), expand=False)
    tbl.add_column(style="dim", justify="right", min_width=13)
    tbl.add_column(justify="left", min_width=16)

    tbl.add_row()
    tbl.add_row("[dim]current rtt[/]",  Text.from_markup(_rtt_markup(cur)))
    tbl.add_row("[dim]average rtt[/]",  Text.from_markup(_rtt_markup(avg)))
    tbl.add_row("[dim]min rtt[/]",      Text.from_markup(_rtt_markup(mn)))
    tbl.add_row("[dim]max rtt[/]",      Text.from_markup(_rtt_markup(mx)))
    tbl.add_row()
    tbl.add_row("[dim]5-min loss[/]",
                Text.from_markup(f"{_loss_markup(l5)}  [dim]({s5:,} pkts)[/]"))
    tbl.add_row("[dim]1-hr  loss[/]",
                Text.from_markup(f"{_loss_markup(l1h)}  [dim]({s1h:,} pkts)[/]"))
    tbl.add_row("[dim]total loss[/]",
                Text.from_markup(_loss_markup(loss_total)))
    tbl.add_row()
    tbl.add_row("[dim]sent[/]",         Text.from_markup(f"[dim]{ts:,}[/]"))
    tbl.add_row("[dim]received[/]",     Text.from_markup(f"[dim]{tr:,}[/]"))
    tbl.add_row("[dim]lost[/]",         Text.from_markup(f"[dim]{ts - tr:,}[/]"))
    tbl.add_row()

    return _panel(Align(tbl, "center", vertical="middle"),
                  "[bold] STATISTICS [/]", down)


def build_visualizer(panel_w: int, panel_h: int) -> Panel:
    with _lock:
        ticker = list(_ticker)
        down   = _net_down

    # Inner grid dimensions (subtract panel border + padding + legend rows)
    cols    = max(4, panel_w - 4)
    rows    = max(2, panel_h - 6)   # reserve 2 rows for legend
    cap     = cols * rows

    recent  = ticker[-cap:] if len(ticker) > cap else ticker
    padding = cap - len(recent)

    empty_s   = Style(color="grey11")
    timeout_s = Style(color="grey30")

    t    = Text()
    flat = [None] * padding + recent
    last = len(flat) - 1   # index of the cursor (newest ping)

    # Fade bands: entries this many steps from the cursor get progressively dimmed.
    # Steps are in units of individual pings (0.2s each).
    # bright = 0-4s, mid = 4-20s, faded = 20-60s, very faded = >60s
    BRIGHT_STEPS = 20    # ~4s
    MID_STEPS    = 100   # ~20s
    FADE_STEPS   = 300   # ~60s

    for i, entry in enumerate(flat):
        if i and i % cols == 0:
            t.append("\n")

        age = last - i  # 0 = cursor (newest), grows toward oldest

        # ── Cursor character (newest ping) ────────────────────────────────
        if i == last and entry is not None:
            base   = _rtt_style(entry['rtt'] if entry['received'] else None)
            color  = base.color.name if base.color else "white"
            if not entry['received']:
                color = "red"
            t.append("▮", style=Style(color=color, bold=True))
            continue

        # ── Fade multiplier based on age ─────────────────────────────────
        if   age <= BRIGHT_STEPS: dim = False;  opacity = "normal"
        elif age <= MID_STEPS:    dim = False;  opacity = "mid"
        elif age <= FADE_STEPS:   dim = True;   opacity = "faded"
        else:                     dim = True;   opacity = "veryfaded"

        # Per-opacity colour overrides for timeout/empty
        def _faded_empty():
            if   opacity == "veryfaded": return Style(color="grey3")
            elif opacity == "faded":     return Style(color="grey7")
            else:                        return Style(color="grey11")

        def _faded_timeout():
            if   opacity == "veryfaded": return Style(color="grey11")
            elif opacity == "faded":     return Style(color="grey19")
            else:                        return Style(color="grey30")

        if entry is None:
            t.append("·", style=_faded_empty())
        elif not entry['received']:
            t.append("░", style=_faded_timeout())
        else:
            base  = _rtt_style(entry['rtt'])
            color = base.color.name if base.color else "white"
            if opacity in ("faded", "veryfaded"):
                # Desaturate: map colours to their dimmed variants
                color = {
                    "bright_green": "dark_green",
                    "green":        "dark_green",
                    "yellow":       "dark_orange",
                    "orange1":      "dark_red",
                    "red":          "dark_red",
                }.get(color, color)
            char = "▒" if opacity in ("faded", "veryfaded") else "█"
            t.append(char, style=Style(color=color, dim=dim))

    # Legend
    t.append("\n\n")
    legend_items = [
        ("▮", "bright_green", "now"),
        ("█", "bright_green", "<20ms"),
        ("█", "green",        "<50ms"),
        ("█", "yellow",       "<100ms"),
        ("█", "orange1",      "<200ms"),
        ("█", "red",          ">200ms"),
        ("░", "grey30",       "timeout"),
    ]
    for char, color, label in legend_items:
        t.append(f" {char}", style=Style(color=color, bold=(char == "▮")))
        t.append(f" {label} ", style=Style(dim=True))

    return _panel(t, "[bold] PING HISTORY [/]", down)


def build_events() -> Panel:
    with _lock:
        down      = _net_down
        attempts  = _down_attempts
        route     = _route
        failovers = list(_failovers)

    t = Text()
    t.append("\n")

    # ── Current route ─────────────────────────────────────────────────
    t.append("  CURRENT ROUTE\n", style=Style(dim=True, bold=True))
    t.append("  " + "─" * 22 + "\n", style=Style(dim=True))

    if route:
        t.append(f"  {route}\n", style=Style(color="cyan", bold=True))
    else:
        t.append("  —\n", style=Style(dim=True))

    if down:
        t.append(f"\n  ● NETWORK DOWN\n", style=Style(color="red", bold=True))
        t.append(f"  retrying... attempt {attempts}\n",
                 style=Style(color="red", dim=True))

    t.append("\n")

    # ── Failover history ──────────────────────────────────────────────
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
                t.append("▼ NETWORK DOWN", style=Style(color="red", bold=True))
                if 'recovered' in evt:
                    secs = evt.get('down_secs', 0)
                    t.append(f"\n         ↑ recovered {evt['recovered']}",
                             style=Style(color="bright_green", dim=True))
                    t.append(f"  ({secs:.1f}s)\n",
                             style=Style(dim=True))
                else:
                    t.append("\n")

            elif etype == 'route':
                frm = evt.get('from', '?')
                to  = evt.get('to',   '?')
                t.append("⇄ WAN FAILOVER\n", style=Style(color="yellow", bold=True))
                t.append(f"         {frm}\n", style=Style(dim=True))
                t.append(f"         → {to}\n", style=Style(color="cyan"))

            t.append("\n")

    return _panel(Align(t, "left"), "[bold] WAN & EVENTS [/]", down)


# ── Layout builder ────────────────────────────────────────────────────────────

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


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    global _target, _target_ip, _running

    if len(sys.argv) < 2:
        sys.exit("Usage: pingx <host>")

    _target = sys.argv[1]
    try:
        _target_ip = socket.gethostbyname(_target)
    except socket.gaierror as e:
        sys.exit(f"pingx: cannot resolve '{_target}': {e}")

    console = Console()

    def _on_exit(sig, frame):
        global _running
        _running = False

    signal.signal(signal.SIGINT, _on_exit)

    threading.Thread(target=_route_monitor, daemon=True).start()
    threading.Thread(target=_ping_loop,     daemon=True).start()

    layout = _make_layout()

    with Live(layout, console=console, screen=True,
              refresh_per_second=REFRESH_HZ) as _live:
        while _running:
            w = console.width
            h = console.height

            # Approximate inner dimensions for the viz panel
            viz_w = max(10, int(w * 3 / 5) - 4)
            viz_h = max(5,  int(h * 3 / 5) - 2)

            layout["logo"].update(build_logo())
            layout["stats"].update(build_stats())
            layout["viz"].update(build_visualizer(viz_w, viz_h))
            layout["events"].update(build_events())

            time.sleep(1 / REFRESH_HZ)

    # ── Post-exit summary ─────────────────────────────────────────────────────
    with _lock:
        ts       = _total_sent
        tr       = _total_recv
        rtt_list = list(_rtts)

    loss = (ts - tr) / ts * 100 if ts else 0.0
    console.print()
    console.print(f"[bold]--- pingx {_target} statistics ---[/]")
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
