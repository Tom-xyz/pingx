# pingx

A full-screen terminal ping monitor with auto-reconnect, WAN failover detection, and a retro TUI.

Standard `ping` stops the moment your network drops and forces you to rerun it. `pingx` keeps going — it detects the outage, retries in the background, and resumes automatically when connectivity returns. It also watches your default route for changes, which surfaces WAN failover events (e.g. fiber → Starlink) as they happen.

## Screenshot

```
╔══════════════════════════════════════╦═══════════════════════════════╗
║  ██████╗ ██╗███╗  ██╗ ██████╗ ██╗  ██╗ ║                               ║
║  ██╔══██╗██║████╗ ██║██╔════╝ ╚██╗██╔╝ ║   current rtt    10.52 ms     ║
║  ██████╔╝██║██╔██╗██║██║  ███╗  ╚███╔╝  ║   average rtt    10.83 ms     ║
║  ██╔═══╝ ██║██║╚████║██║   ██║  ██╔██╗  ║   min rtt         8.90 ms     ║
║  ██║     ██║██║ ╚███║╚██████╔╝ ██╔╝╚██╗ ║   max rtt        15.70 ms     ║
║  ╚═╝     ╚═╝╚═╝  ╚══╝ ╚═════╝ ╚═╝  ╚═╝ ║                               ║
║                                          ║   5-min loss    0.0%          ║
║  ● CONNECTED                             ║   1-hr  loss    0.0%          ║
║  host   9.9.9.9  (9.9.9.9)              ║   total loss    0.0%          ║
║  route  192.168.100.1                    ║                               ║
║  uptime 00:04:22                         ║   sent       1,320            ║
╠══════════════════════════════════════╬═══════════════════════════════╣
║ ██████████████████████████████████  ║  CURRENT ROUTE                ║
║ ██████████████████████████████████  ║  ───────────────────           ║
║ ████████████░░░░████████████████    ║  192.168.100.1                ║
║ ████████████████████████████████    ║                               ║
║                                      ║  FAILOVER HISTORY             ║
║  █<20ms  █<50ms  █<100ms  █>200ms   ║  ───────────────────           ║
║  ░timeout                            ║  no events yet                ║
╚══════════════════════════════════════╩═══════════════════════════════╝
```

## Features

- **Auto-reconnect** — detects network down after 5 consecutive timeouts, retries in the background, resumes automatically on recovery
- **WAN failover detection** — polls the default route every 3 seconds; announces gateway changes (e.g. `192.168.100.1 → 192.168.1.1`) as they happen
- **Ping heatmap** — rolling grid of recent pings coloured by RTT; fills the panel and adapts to terminal size
- **Live RTT stats** — current / average / min / max updated every ping
- **Rolling loss windows** — packet loss over the last 5 minutes and last 1 hour, plus all-time
- **Failover event log** — last 2 WAN events with timestamps and recovery durations
- **Dynamic layout** — four-quadrant TUI that reflows on terminal resize
- **No root required** — uses unprivileged ICMP (`SOCK_DGRAM`) on macOS

## Requirements

- macOS (see [Platform support](#platform-support))
- Python 3.10+
- [rich](https://github.com/Textualize/rich)

```
pip install rich
```

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/pingx.git
cd pingx
pip install -r requirements.txt

# Add to PATH (symlink or copy)
ln -s "$PWD/pingx.py" /usr/local/bin/pingx
chmod +x /usr/local/bin/pingx
```

## Usage

```
pingx <host>
```

```bash
pingx 9.9.9.9
pingx google.com
pingx 192.168.90.1     # ping your router
```

Press `Ctrl-C` to exit. A final summary (packets transmitted/received, loss %, min/avg/max/stddev RTT) is printed on exit.

## Ping heatmap colours

| Colour | Meaning |
|--------|---------|
| **bright green** `█` | < 20 ms |
| green `█` | 20 – 50 ms |
| yellow `█` | 50 – 100 ms |
| orange `█` | 100 – 200 ms |
| red `█` | > 200 ms |
| grey `░` | timeout / lost |
| · | no data yet |

## Platform support

`pingx` uses `socket.SOCK_DGRAM` with `IPPROTO_ICMP`, which macOS supports without elevated privileges.

**Linux** requires either:
- `sudo` (raw socket), or
- Setting the ping group range: `sudo sysctl -w net.ipv4.ping_group_range="0 65535"`

**Windows** is not currently supported.

## License

MIT
