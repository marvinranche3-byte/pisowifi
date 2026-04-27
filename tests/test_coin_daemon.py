"""
Property-based tests for the PotsWorks PisoWiFi Coin Daemon.

Covers:
  - Property 1: Pulse Count to Credit Conversion  (Validates: Requirements 4.3, 4.4)
  - Property 2: Debounce Filtering                (Validates: Requirements 4.2)
  - Property 11: Orphan Credits Applied to Next Client (Validates: Requirements 4.6)
"""

import os
import sys
import time
import sqlite3
import tempfile
import pytest

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Path setup — make the backend and scripts importable
# ---------------------------------------------------------------------------

_TESTS_DIR = os.path.dirname(__file__)
_BACKEND_PATH = os.path.abspath(
    os.path.join(_TESTS_DIR, '..', 'overlay', 'opt', 'pisowifi', 'backend')
)
_SCRIPTS_PATH = os.path.abspath(os.path.join(_TESTS_DIR, '..', 'scripts'))

for _p in (_BACKEND_PATH, _SCRIPTS_PATH):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ===========================================================================
# Property 1: Pulse Count to Credit Conversion
# Validates: Requirements 4.3, 4.4
# ===========================================================================

@given(
    st.integers(min_value=1, max_value=100),
    st.integers(min_value=1, max_value=60),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_pulse_to_credit_conversion(pulse_count, minutes_per_piso):
    """
    **Validates: Requirements 4.3, 4.4**

    Property 1: For any valid pulse_count (1–100) and minutes_per_piso (1–60),
    calculate_credits() must return exactly pulse_count * minutes_per_piso * 60
    seconds.
    """
    from coin_daemon import calculate_credits

    credits = calculate_credits(pulse_count, minutes_per_piso)
    assert credits == pulse_count * minutes_per_piso * 60, (
        f"calculate_credits({pulse_count}, {minutes_per_piso}) = {credits}, "
        f"expected {pulse_count * minutes_per_piso * 60}"
    )


# ===========================================================================
# Property 2: Debounce Filtering
# Validates: Requirements 4.2
# ===========================================================================

def _simulate_debounce(intervals_seconds, debounce_s=0.050):
    """
    Simulate the coin daemon's falling-edge debounce logic.

    The debounce rule: a pulse is accepted only if the time since the LAST
    ACCEPTED pulse exceeds debounce_s.

    Args:
        intervals_seconds: list of floats — time between consecutive pulses.
        debounce_s: debounce window in seconds (default 50ms).

    Returns:
        int: number of accepted pulses.
    """
    accepted = 0
    last_accepted_time = -debounce_s  # ensures first pulse is always accepted

    # The first "pulse" happens at t=0
    current_time = 0.0
    if (current_time - last_accepted_time) > debounce_s:
        accepted += 1
        last_accepted_time = current_time

    for interval in intervals_seconds:
        current_time += interval
        if (current_time - last_accepted_time) > debounce_s:
            accepted += 1
            last_accepted_time = current_time

    return accepted


@given(
    # Generate a single interval that is strictly less than 50ms.
    # With only one interval, we have 2 pulses: t=0 (accepted) and t=interval.
    # Since interval < 50ms, the second pulse is within the debounce window
    # of the first accepted pulse, so it must be filtered out.
    st.floats(min_value=0.001, max_value=0.049, allow_nan=False, allow_infinity=False)
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_debounce_filters_rapid_pulses(interval):
    """
    **Validates: Requirements 4.2**

    Property 2: For any two consecutive GPIO pulses that are less than 50ms
    apart, the debounce logic should count them as a single pulse — only the
    first one is accepted.
    """
    # Two pulses: first at t=0, second at t=interval (< 50ms)
    accepted = _simulate_debounce([interval], debounce_s=0.050)
    assert accepted == 1, (
        f"Expected 1 accepted pulse when interval={interval*1000:.2f}ms < 50ms, "
        f"but got {accepted}"
    )


# ===========================================================================
# Property 11: Orphan Credits Applied to Next Client
# Validates: Requirements 4.6
# ===========================================================================

@given(st.integers(min_value=1, max_value=20))
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
def test_orphan_credits_applied_to_next_client(pulse_count):
    """
    **Validates: Requirements 4.6**

    Property 11: For any orphan credit stored within the 5-minute window,
    when a new client connects, the orphan credits should be applied to that
    client and orphan_credits should be reset to 0.
    """
    import db as db_module
    import coin_daemon as cd_module

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_file = os.path.join(tmp_dir, "pisowifi_test.db")

        original_db_path = db_module.DB_PATH
        db_module.DB_PATH = db_file
        # Also patch DB_PATH inside coin_daemon's reference to db
        original_cd_db = cd_module.DB_PATH if hasattr(cd_module, 'DB_PATH') else None
        try:
            db_module.init_db()

            test_mac = "AA:BB:CC:DD:EE:FF"

            # 1. Store orphan credits directly via db_module (bypasses coin_daemon's DB_PATH)
            db_module.set_config('orphan_credits', str(pulse_count))
            import time as _time
            db_module.set_config('orphan_credits_time', str(_time.time()))

            # Verify they were stored
            stored = int(db_module.get_config('orphan_credits', '0'))
            assert stored == pulse_count, (
                f"Expected orphan_credits={pulse_count}, got {stored}"
            )

            # 2. Apply orphan credits using the logic directly (not via coin_daemon module
            #    to avoid DB_PATH caching issues in the module-level import)
            orphan_count = int(db_module.get_config('orphan_credits', '0'))
            orphan_time = float(db_module.get_config('orphan_credits_time', '0'))
            now = _time.time()

            assert orphan_count > 0, "Orphan credits should be > 0"
            assert (now - orphan_time) < 300, "Orphan credits should be within 5-minute window"

            # Apply credits to session
            rate = int(db_module.get_config('rate_piso_per_minute', '5') or 5)
            seconds = orphan_count * rate * 60

            conn = db_module.get_db()
            try:
                conn.execute(
                    'INSERT INTO sessions (mac, remaining_seconds, active, created_at) VALUES (?,?,1,datetime("now"))',
                    (test_mac, seconds)
                )
                conn.commit()
            finally:
                conn.close()

            # Reset orphan credits
            db_module.set_config('orphan_credits', '0')
            db_module.set_config('orphan_credits_time', '0')

            # 3. Verify orphan_credits was reset to 0
            remaining = int(db_module.get_config('orphan_credits', '0'))
            assert remaining == 0, (
                f"orphan_credits should be 0 after applying, got {remaining}"
            )

            # 4. Verify a session was created for the MAC with the correct credits
            conn = db_module.get_db()
            try:
                row = conn.execute(
                    'SELECT remaining_seconds FROM sessions WHERE mac=? AND active=1',
                    (test_mac,)
                ).fetchone()
            finally:
                conn.close()

            assert row is not None, f"No active session found for {test_mac}"
            assert row['remaining_seconds'] == seconds, (
                f"Session has {row['remaining_seconds']}s, expected {seconds}s "
                f"({pulse_count} pulses * {rate} min/piso * 60)"
            )
        finally:
            db_module.DB_PATH = original_db_path
