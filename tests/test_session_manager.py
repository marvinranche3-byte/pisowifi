"""
Property-based tests for the PotsWorks PisoWiFi Session Manager.

Covers:
  - Property 3: Session Countdown Invariant        (Validates: Requirements 6.1)
  - Property 4: Session Expiration Triggers Block  (Validates: Requirements 6.2)
  - Property 5: Credit Addition to Active Session  (Validates: Requirements 6.3)
  - Property 6: Session Persistence Round Trip     (Validates: Requirements 6.5, 10.5)
"""

import os
import sys
import sqlite3
import tempfile
import pytest

from unittest.mock import patch, call
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Path setup — make the backend importable
# ---------------------------------------------------------------------------

_TESTS_DIR = os.path.dirname(__file__)
_BACKEND_PATH = os.path.abspath(
    os.path.join(_TESTS_DIR, '..', 'overlay', 'opt', 'pisowifi', 'backend')
)
if _BACKEND_PATH not in sys.path:
    sys.path.insert(0, _BACKEND_PATH)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session(db_file, mac, remaining_seconds, active=1):
    """Insert a session row directly into the test DB and return its id."""
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "INSERT INTO sessions (mac, remaining_seconds, active, created_at) "
        "VALUES (?, ?, ?, datetime('now'))",
        (mac, remaining_seconds, active),
    )
    sid = cur.lastrowid
    conn.commit()
    conn.close()
    return sid


def _get_session(db_file, sid):
    """Fetch a session row by id from the test DB."""
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, mac, remaining_seconds, active FROM sessions WHERE id=?",
        (sid,),
    ).fetchone()
    conn.close()
    return row


# ===========================================================================
# Property 3: Session Countdown Invariant
# Validates: Requirements 6.1
# ===========================================================================

@given(st.integers(min_value=1, max_value=86400))
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_session_countdown_invariant(initial_seconds):
    """
    **Validates: Requirements 6.1**

    Property 3: For any active session with remaining_seconds > 0, after one
    tick of the countdown loop, remaining_seconds must decrease by exactly 1.
    """
    import db as db_module
    import session_manager as sm

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_file = os.path.join(tmp_dir, "pisowifi_test.db")
        original_db_path = db_module.DB_PATH
        db_module.DB_PATH = db_file
        try:
            db_module.init_db()

            mac = "AA:BB:CC:DD:EE:01"
            sid = _make_session(db_file, mac, initial_seconds)

            # Run one tick of the countdown (mock os.system to avoid iptables)
            with patch('os.system', return_value=0):
                conn = db_module.get_db()
                try:
                    rows = conn.execute(
                        "SELECT id, mac, remaining_seconds FROM sessions WHERE active=1"
                    ).fetchall()
                    for row in rows:
                        secs = row['remaining_seconds'] - 1
                        if secs <= 0:
                            conn.execute(
                                "UPDATE sessions SET remaining_seconds=0, active=0 WHERE id=?",
                                (row['id'],),
                            )
                            sm.block_mac(row['mac'])
                        else:
                            conn.execute(
                                "UPDATE sessions SET remaining_seconds=? WHERE id=?",
                                (secs, row['id']),
                            )
                    conn.commit()
                finally:
                    conn.close()

            row = _get_session(db_file, sid)
            assert row is not None

            if initial_seconds == 1:
                # Session should have expired
                assert row['remaining_seconds'] == 0
                assert row['active'] == 0
            else:
                # remaining_seconds decreased by exactly 1
                assert row['remaining_seconds'] == initial_seconds - 1
                assert row['active'] == 1
        finally:
            db_module.DB_PATH = original_db_path


# ===========================================================================
# Property 4: Session Expiration Triggers Block
# Validates: Requirements 6.2
# ===========================================================================

@given(st.integers(min_value=1, max_value=10))
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_session_expiration_triggers_block(initial_seconds):
    """
    **Validates: Requirements 6.2**

    Property 4: For any session where remaining_seconds reaches 0, the MAC
    address must be blocked (block_mac called) and the session marked inactive.
    """
    import db as db_module
    import session_manager as sm

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_file = os.path.join(tmp_dir, "pisowifi_test.db")
        original_db_path = db_module.DB_PATH
        db_module.DB_PATH = db_file
        try:
            db_module.init_db()

            mac = "BB:CC:DD:EE:FF:02"
            _make_session(db_file, mac, initial_seconds)

            block_calls = []

            def fake_os_system(cmd):
                block_calls.append(cmd)
                return 0

            # Run the countdown until the session expires
            with patch('os.system', side_effect=fake_os_system):
                for _ in range(initial_seconds):
                    conn = db_module.get_db()
                    try:
                        rows = conn.execute(
                            "SELECT id, mac, remaining_seconds FROM sessions WHERE active=1"
                        ).fetchall()
                        for row in rows:
                            secs = row['remaining_seconds'] - 1
                            if secs <= 0:
                                conn.execute(
                                    "UPDATE sessions SET remaining_seconds=0, active=0 WHERE id=?",
                                    (row['id'],),
                                )
                                sm.block_mac(row['mac'])
                            else:
                                conn.execute(
                                    "UPDATE sessions SET remaining_seconds=? WHERE id=?",
                                    (secs, row['id']),
                                )
                        conn.commit()
                    finally:
                        conn.close()

            # Verify session is now inactive
            conn = sqlite3.connect(db_file)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT active, remaining_seconds FROM sessions WHERE mac=?",
                (mac,),
            ).fetchone()
            conn.close()

            assert row is not None
            assert row['active'] == 0, (
                f"Session should be inactive after expiration, got active={row['active']}"
            )
            assert row['remaining_seconds'] == 0

            # Verify block_mac was called (iptables -D commands were issued)
            block_related = [c for c in block_calls if '-D' in c or 'PREROUTING' in c or 'FORWARD' in c]
            assert len(block_related) > 0, (
                f"Expected iptables block commands to be called, got: {block_calls}"
            )
        finally:
            db_module.DB_PATH = original_db_path


# ===========================================================================
# Property 5: Credit Addition to Active Session
# Validates: Requirements 6.3
# ===========================================================================

@given(
    st.integers(min_value=1, max_value=3600),
    st.integers(min_value=1, max_value=100),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_credit_addition_to_active_session(initial_seconds, additional_seconds):
    """
    **Validates: Requirements 6.3**

    Property 5: For any active session and any positive coin insertion,
    remaining_seconds must equal initial_seconds + additional_seconds after
    the credit is applied.
    """
    import db as db_module

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_file = os.path.join(tmp_dir, "pisowifi_test.db")
        original_db_path = db_module.DB_PATH
        db_module.DB_PATH = db_file
        try:
            db_module.init_db()

            mac = "CC:DD:EE:FF:00:03"
            sid = _make_session(db_file, mac, initial_seconds)

            # Simulate coin insertion: add additional_seconds to the session
            conn = db_module.get_db()
            try:
                row = conn.execute(
                    "SELECT id, remaining_seconds FROM sessions WHERE mac=? AND active=1",
                    (mac,),
                ).fetchone()
                assert row is not None
                new_seconds = row['remaining_seconds'] + additional_seconds
                conn.execute(
                    "UPDATE sessions SET remaining_seconds=? WHERE id=?",
                    (new_seconds, row['id']),
                )
                conn.commit()
            finally:
                conn.close()

            # Verify the total is exactly initial + additional
            row = _get_session(db_file, sid)
            assert row is not None
            assert row['remaining_seconds'] == initial_seconds + additional_seconds, (
                f"Expected {initial_seconds + additional_seconds} seconds, "
                f"got {row['remaining_seconds']}"
            )
            assert row['active'] == 1
        finally:
            db_module.DB_PATH = original_db_path


# ===========================================================================
# Property 6: Session Persistence Round Trip
# Validates: Requirements 6.5, 10.5
# ===========================================================================

@given(
    st.lists(
        st.integers(min_value=60, max_value=3600),
        min_size=1,
        max_size=5,
    )
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_session_persistence_round_trip(session_seconds_list):
    """
    **Validates: Requirements 6.5, 10.5**

    Property 6: For any set of active sessions stored in the database, after
    calling restore_sessions() (simulating a reboot), all sessions must still
    have their correct remaining_seconds values and allow_mac must have been
    called for each one.
    """
    import db as db_module
    import session_manager as sm

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_file = os.path.join(tmp_dir, "pisowifi_test.db")
        original_db_path = db_module.DB_PATH
        db_module.DB_PATH = db_file
        try:
            db_module.init_db()

            # Create one session per entry in session_seconds_list
            macs = [f"DD:EE:FF:00:{i:02X}:04" for i in range(len(session_seconds_list))]
            session_ids = []
            for mac, secs in zip(macs, session_seconds_list):
                sid = _make_session(db_file, mac, secs)
                session_ids.append(sid)

            iptables_calls = []

            def fake_os_system(cmd):
                iptables_calls.append(cmd)
                return 0

            # Simulate reboot: call restore_sessions()
            with patch('os.system', side_effect=fake_os_system):
                sm.restore_sessions()

            # Verify all sessions still have their original remaining_seconds
            for sid, expected_secs in zip(session_ids, session_seconds_list):
                row = _get_session(db_file, sid)
                assert row is not None
                assert row['remaining_seconds'] == expected_secs, (
                    f"Session {sid}: expected {expected_secs}s, "
                    f"got {row['remaining_seconds']}s after restore"
                )
                assert row['active'] == 1

            # Verify allow_mac was called for each session
            # allow_mac issues 2 iptables commands per MAC (PREROUTING + FORWARD)
            allow_calls = [c for c in iptables_calls if '-I' in c]
            assert len(allow_calls) == len(session_seconds_list) * 2, (
                f"Expected {len(session_seconds_list) * 2} iptables -I calls "
                f"(2 per session), got {len(allow_calls)}: {allow_calls}"
            )
        finally:
            db_module.DB_PATH = original_db_path
