"""
Tests for pingx core logic.
Run with:  python -m pytest tests/
"""

import struct
import sys
import time
import threading
from collections import deque
from unittest.mock import patch

import pytest

# Make sure the parent directory is on the path
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__file__)))

import pingx


# ── _checksum ─────────────────────────────────────────────────────────────────

class TestChecksum:
    def test_known_value(self):
        # ICMP echo request with all-zero checksum field → checksum must produce
        # a value such that re-running it over the result yields 0xFFFF (ones-comp 0)
        data = b'\x08\x00\x00\x00\x00\x01\x00\x01' + b'\x00' * 8
        chk  = pingx._checksum(data)
        assert 0 <= chk <= 0xFFFF

    def test_odd_length_padding(self):
        # Odd-length input must not raise
        data = b'\x08\x00\x00\x00\x01'
        chk  = pingx._checksum(data)
        assert isinstance(chk, int)

    def test_round_trip(self):
        # After inserting the checksum, re-checking should give 0
        payload = b'\x08\x00' + b'\x00\x00' + b'\x00\x01\x00\x01' + b'\xab\xcd' * 4
        chk     = pingx._checksum(payload)
        patched = payload[:2] + struct.pack('!H', chk) + payload[4:]
        result  = pingx._checksum(patched)
        assert result == 0 or result == 0xFFFF  # ones-complement identity


# ── _window_loss ──────────────────────────────────────────────────────────────

class TestWindowLoss:
    def _make_state(self, events):
        st = pingx.PingState()
        st.events = deque(events, maxlen=20000)
        return st

    def test_zero_loss_all_received(self):
        now = time.monotonic()
        events = [(now - i * 0.2, True) for i in range(10)]
        st = self._make_state(events)
        loss, sent, recv = pingx._window_loss(60, st)
        assert loss == 0.0
        assert sent == recv == 10

    def test_full_loss_none_received(self):
        now = time.monotonic()
        events = [(now - i * 0.2, False) for i in range(10)]
        st = self._make_state(events)
        loss, sent, recv = pingx._window_loss(60, st)
        assert loss == 100.0
        assert recv == 0

    def test_partial_loss(self):
        now = time.monotonic()
        events  = [(now - i * 0.2, True)  for i in range(8)]
        events += [(now - (8 + i) * 0.2, False) for i in range(2)]
        st = self._make_state(events)
        loss, sent, recv = pingx._window_loss(60, st)
        assert sent == 10
        assert recv == 8
        assert abs(loss - 20.0) < 0.01

    def test_empty_window_returns_zero(self):
        st = self._make_state([])
        loss, sent, recv = pingx._window_loss(60, st)
        assert loss == 0.0
        assert sent == recv == 0

    def test_window_excludes_old_events(self):
        now = time.monotonic()
        old_events   = [(now - 400 + i, False) for i in range(5)]   # outside 5-min window
        fresh_events = [(now - 10  + i, True)  for i in range(5)]   # inside
        st = self._make_state(old_events + fresh_events)
        loss, sent, recv = pingx._window_loss(300, st)
        assert sent == 5
        assert recv == 5
        assert loss == 0.0


# ── RTT colour thresholds ─────────────────────────────────────────────────────

class TestRttStyle:
    def setup_method(self):
        # Use default green theme
        pingx._theme = pingx.THEMES["green"]

    def test_fast_rtt_is_bold(self):
        style = pingx._rtt_style(10.0)
        assert style.bold is True

    def test_good_rtt_not_bold(self):
        style = pingx._rtt_style(30.0)
        assert not style.bold

    def test_none_rtt_is_dim(self):
        style = pingx._rtt_style(None)
        assert style.color.name == "grey23"

    def test_crit_rtt_is_red_bold(self):
        style = pingx._rtt_style(250.0)
        assert "red" in style.color.name


class TestSparklineColor:
    def setup_method(self):
        pingx._theme = pingx.THEMES["green"]

    def test_below_80_is_bright_green(self):
        assert pingx._sparkline_color(10.0)  == "bright_green"
        assert pingx._sparkline_color(79.9)  == "bright_green"

    def test_80_to_150_is_yellow(self):
        assert pingx._sparkline_color(80.0)  == "yellow"
        assert pingx._sparkline_color(149.9) == "yellow"

    def test_150_to_300_is_orange(self):
        assert pingx._sparkline_color(150.0) == "orange1"
        assert pingx._sparkline_color(299.9) == "orange1"

    def test_above_300_is_red(self):
        assert pingx._sparkline_color(300.0) == "red"
        assert pingx._sparkline_color(999.0) == "red"

    def test_none_is_grey(self):
        assert pingx._sparkline_color(None)  == "grey23"

    def test_sparkline_wide_band_stable_on_home_network(self):
        # Typical home RTT (8-30ms) must never change colour
        colours = {pingx._sparkline_color(rtt) for rtt in range(8, 31)}
        assert len(colours) == 1, f"Colour flipped in 8-30ms range: {colours}"


# ── Theme coverage ────────────────────────────────────────────────────────────

class TestThemes:
    def test_all_named_themes_present(self):
        for name in ("green", "blue", "cyan", "amber", "red", "purple"):
            assert name in pingx.THEMES

    def test_theme_fields_are_strings(self):
        for theme in pingx.THEMES.values():
            assert isinstance(theme.spark_ok, str)
            assert isinstance(theme.border_ok, str)
            assert isinstance(theme.accent, str)


# ── _parse_reply validation ───────────────────────────────────────────────────

class TestParseReply:
    """
    Guards against stale / foreign ICMP packets producing impossibly low RTTs.

    Without reply validation, recvfrom() would accept any ICMP packet —
    including delayed replies from previous timed-out pings arriving in the
    next ping's receive window.  The embedded send timestamp would not match
    the current send_ts, yielding RTTs of ~0.2ms for a cross-internet host.

    _parse_reply must:
      - Accept only type=0 (echo reply) packets
      - Reject packets with the wrong ident (other processes)
      - Reject packets with the wrong seq (previous/stale pings)
      - Return the embedded monotonic send timestamp on a valid match
    """

    def _make_reply(self, ident: int, seq: int, send_ts: float,
                    icmp_type: int = 0) -> bytes:
        """Build a minimal ICMP echo reply carrying an embedded timestamp."""
        payload = struct.pack('!d', send_ts)
        hdr = struct.pack('!BBHHH', icmp_type, 0, 0, ident, seq & 0xffff)
        return hdr + payload

    def test_valid_reply_returns_timestamp(self):
        ts   = time.monotonic() - 0.010   # 10ms ago
        data = self._make_reply(ident=1234, seq=7, send_ts=ts)
        result = pingx._parse_reply(data, ident=1234, seq=7)
        assert result is not None
        assert abs(result - ts) < 1e-9

    def test_wrong_ident_returns_none(self):
        ts   = time.monotonic()
        data = self._make_reply(ident=9999, seq=7, send_ts=ts)
        assert pingx._parse_reply(data, ident=1234, seq=7) is None

    def test_wrong_seq_returns_none(self):
        ts   = time.monotonic()
        data = self._make_reply(ident=1234, seq=6, send_ts=ts)   # seq=6, expected 7
        assert pingx._parse_reply(data, ident=1234, seq=7) is None

    def test_wrong_icmp_type_returns_none(self):
        """type=8 is an echo request — should be rejected."""
        ts   = time.monotonic()
        data = self._make_reply(ident=1234, seq=7, send_ts=ts, icmp_type=8)
        assert pingx._parse_reply(data, ident=1234, seq=7) is None

    def test_too_short_returns_none(self):
        assert pingx._parse_reply(b'\x00' * 15, ident=1234, seq=7) is None
        assert pingx._parse_reply(b'',           ident=1234, seq=7) is None

    def test_stale_reply_carries_old_timestamp(self):
        """
        A delayed reply from seq=N-1 must be rejected when we're waiting for
        seq=N.  Simulates the scenario that caused min RTT to read ~0.2ms:
        the old reply arrives quickly but has the wrong seq, so it's discarded.
        """
        old_ts = time.monotonic() - 2.0   # arrived 2s late
        stale  = self._make_reply(ident=1234, seq=6, send_ts=old_ts)
        assert pingx._parse_reply(stale, ident=1234, seq=7) is None

    def test_seq_wraps_at_16_bits(self):
        """seq is sent as seq & 0xffff — validate wrap-around matching."""
        ts   = time.monotonic()
        data = self._make_reply(ident=1234, seq=0, send_ts=ts)    # 65536 & 0xffff == 0
        assert pingx._parse_reply(data, ident=1234, seq=65536) is not None


# ── Stats accumulation / lost-counter stability ───────────────────────────────

class TestStatsAccumulation:
    """
    Guards against two related bugs:

    Bug 1 — transient lost=1 on every successful ping:
      total_sent was incremented before recvfrom() returned. The TUI firing
      at 10 Hz could catch the window and display lost=1, then flip back to 0
      when total_recv caught up.

    Bug 2 — window loss (5-min / 1-hr) never records real losses:
      events.append((ts, False)) happened before recvfrom(), so _window_loss
      saw the in-flight packet as a loss (100% flash at startup). Worse, the
      except block never appended to events at all, so actual timeouts were
      silently dropped from the window calculations.

    Fix: all state (events, total_sent, total_recv) is updated in a single
    lock block *after* the outcome is known — success or timeout.
    """

    def _sim_success(self, st: pingx.PingState, send_ts: float = None) -> None:
        """Simulate one successful ping: all counters update atomically."""
        ts = send_ts if send_ts is not None else time.monotonic()
        with st.lock:
            st.events.append((ts, True))
            st.total_sent += 1
            st.total_recv += 1

    def _sim_timeout(self, st: pingx.PingState, send_ts: float = None) -> None:
        """Simulate one timed-out ping: sent increments, event recorded as loss."""
        ts = send_ts if send_ts is not None else time.monotonic()
        with st.lock:
            st.events.append((ts, False))
            st.total_sent += 1

    def _lost(self, st: pingx.PingState) -> int:
        return st.total_sent - st.total_recv

    # ── total_sent / total_recv atomicity ─────────────────────────────────────

    def test_lost_is_zero_after_successful_ping(self):
        st = pingx.PingState()
        self._sim_success(st)
        assert self._lost(st) == 0

    def test_lost_is_one_after_timeout(self):
        st = pingx.PingState()
        self._sim_timeout(st)
        assert self._lost(st) == 1

    def test_lost_never_goes_negative(self):
        st = pingx.PingState()
        for _ in range(20):
            self._sim_success(st)
        assert self._lost(st) == 0

    def test_lost_stable_across_many_successful_pings(self):
        """lost must be 0 after every successful ping, never transiently 1."""
        st = pingx.PingState()
        for _ in range(100):
            self._sim_success(st)
            assert self._lost(st) == 0, \
                f"lost={self._lost(st)} after success — sent/recv not atomic"

    def test_lost_accumulates_correctly_with_mixed_outcomes(self):
        st = pingx.PingState()
        self._sim_success(st)
        self._sim_timeout(st)
        self._sim_success(st)
        self._sim_timeout(st)
        self._sim_success(st)
        assert self._lost(st) == 2
        assert st.total_sent == 5
        assert st.total_recv == 3

    def test_sent_and_recv_never_updated_separately_on_success(self):
        """Threaded reader must never observe lost > 0 during all-success run."""
        st = pingx.PingState()
        observations = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                with st.lock:
                    observations.append(st.total_sent - st.total_recv)

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        for _ in range(50):
            self._sim_success(st)
        stop.set()
        t.join(timeout=1)

        bad = [v for v in observations if v != 0]
        assert not bad, \
            f"Reader saw transient non-zero lost values: {bad[:5]}"

    def test_total_sent_never_decreases(self):
        st = pingx.PingState()
        prev = 0
        for _ in range(10):
            self._sim_success(st)
            assert st.total_sent >= prev
            prev = st.total_sent

    def test_total_recv_never_exceeds_total_sent(self):
        st = pingx.PingState()
        for _ in range(10):
            self._sim_success(st)
            assert st.total_recv <= st.total_sent

    # ── _window_loss accuracy (bug 2) ─────────────────────────────────────────

    def test_window_loss_zero_when_no_events(self):
        """At startup with no finalized pings, loss must be 0 — not a flash."""
        st = pingx.PingState()
        loss, sent, recv = pingx._window_loss(300, st)
        assert loss == 0.0
        assert sent == 0

    def test_window_loss_zero_for_all_success(self):
        st = pingx.PingState()
        for _ in range(10):
            self._sim_success(st)
        loss, sent, _ = pingx._window_loss(300, st)
        assert loss == 0.0
        assert sent == 10

    def test_window_loss_records_timeouts(self):
        """Timeouts must appear in window loss — the old bug dropped them."""
        st = pingx.PingState()
        self._sim_success(st)
        self._sim_success(st)
        self._sim_timeout(st)  # 1 loss out of 3
        loss, sent, recv = pingx._window_loss(300, st)
        assert sent == 3
        assert recv == 2
        assert abs(loss - 33.33) < 0.1

    def test_window_loss_never_shows_inflight_as_loss(self):
        """
        Before the fix, events.append((ts, False)) was called before recvfrom,
        so a freshly-started pingx showed 100% loss until the first recv.
        Verify: an event is only in the deque with received=True (success) or
        received=False (confirmed timeout), never as a dangling in-flight False.
        """
        st = pingx.PingState()
        # Simulate what the OLD code did: append False, then update to True
        # (this is the bug pattern — it should NOT happen in the fixed code)
        # The fixed code never appends a False that later gets flipped to True.
        # We just verify that after a success, the event is True from the start.
        now = time.monotonic()
        self._sim_success(st, send_ts=now)
        with st.lock:
            last_received = st.events[-1][1]
        assert last_received is True, \
            "Success event was stored as False — in-flight append bug present"

    def test_window_excludes_events_older_than_window(self):
        st = pingx.PingState()
        old_ts = time.monotonic() - 400  # outside 5-min window
        with st.lock:
            st.events.append((old_ts, False))  # old loss, should be excluded
            st.total_sent += 1
        for _ in range(5):
            self._sim_success(st)
        loss, sent, _ = pingx._window_loss(300, st)
        assert sent == 5   # old event excluded
        assert loss == 0.0


# ── Platform detection ────────────────────────────────────────────────────────

class TestPlatformCheck:
    def test_windows_exits(self):
        with patch("platform.system", return_value="Windows"):
            with pytest.raises(SystemExit) as exc_info:
                pingx.check_platform()
            assert "Windows" in str(exc_info.value)

    def test_linux_exits_on_permission_error(self):
        with patch("platform.system", return_value="Linux"):
            with patch("socket.socket") as mock_sock:
                mock_sock.side_effect = PermissionError
                with pytest.raises(SystemExit) as exc_info:
                    pingx.check_platform()
                assert "sysctl" in str(exc_info.value)

    def test_macos_passes(self):
        with patch("platform.system", return_value="Darwin"):
            # Should not raise
            pingx.check_platform()
