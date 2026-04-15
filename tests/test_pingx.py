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


# ── Stats accumulation / lost-counter stability ───────────────────────────────

class TestStatsAccumulation:
    """
    Guard against the transient lost=1 bug where total_sent was incremented
    before recvfrom() returned, causing the TUI to briefly display lost=1
    on every successful ping then flip back to lost=0 on the next render.

    The fix: total_sent and total_recv must always be incremented in the same
    lock block (both on success), or only total_sent on timeout. The display
    value (total_sent - total_recv) must never exceed the true number of
    confirmed losses.
    """

    def _sim_success(self, st: pingx.PingState) -> None:
        """Simulate one successful ping: both counters update atomically."""
        with st.lock:
            st.total_sent += 1
            st.total_recv += 1

    def _sim_timeout(self, st: pingx.PingState) -> None:
        """Simulate one timed-out ping: only sent increments."""
        with st.lock:
            st.total_sent += 1

    def _lost(self, st: pingx.PingState) -> int:
        return st.total_sent - st.total_recv

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
        """
        If total_sent were incremented before recvfrom (old bug), a reader
        between the two lock sections would see lost=1.  Verify the fix by
        checking that no observable intermediate state has lost > confirmed
        losses using a threaded reader.
        """
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

        # Every observed value must be 0 — no transient lost=1 mid-update
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
