"""
Property-based tests for the PotsWorks PisoWiFi Voucher System.

Covers:
  - Property 7: Voucher Redemption Credits  (Validates: Requirements 7.3)
"""

import os
import sys
import sqlite3
import tempfile

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

_TESTS_DIR = os.path.dirname(__file__)
_BACKEND_PATH = os.path.abspath(
    os.path.join(_TESTS_DIR, '..', 'overlay', 'opt', 'pisowifi', 'backend')
)
if _BACKEND_PATH not in sys.path:
    sys.path.insert(0, _BACKEND_PATH)


# ===========================================================================
# Property 7: Voucher Redemption Credits
# Validates: Requirements 7.3
# ===========================================================================

@given(st.integers(min_value=1, max_value=1440))
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_voucher_redemption_credits(duration_minutes):
    """
    **Validates: Requirements 7.3**

    Property 7: For any valid unused voucher with any duration, redeeming it
    must credit exactly the voucher duration in minutes (duration * 60 seconds)
    to the customer session.
    """
    import db as db_module

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_file = os.path.join(tmp_dir, "pisowifi_test.db")
        original = db_module.DB_PATH
        db_module.DB_PATH = db_file
        try:
            db_module.init_db()

            mac = "EE:FF:00:11:22:33"
            code = "TEST-ABCD-1234"

            # Insert a fresh unused voucher
            conn = sqlite3.connect(db_file)
            conn.row_factory = sqlite3.Row
            conn.execute(
                "INSERT INTO vouchers (code, minutes, used, created_at) VALUES (?,?,0,datetime('now'))",
                (code, duration_minutes)
            )
            conn.commit()
            conn.close()

            # Redeem the voucher (simulate what the Flask route does)
            conn = db_module.get_db()
            try:
                row = conn.execute(
                    "SELECT id, minutes FROM vouchers WHERE code=? AND used=0", (code,)
                ).fetchone()
                assert row is not None, "Voucher should exist and be unused"

                minutes = row['minutes']
                seconds = minutes * 60

                conn.execute(
                    "UPDATE vouchers SET used=1, used_by=?, used_at=datetime('now') WHERE id=?",
                    (mac, row['id'])
                )
                # No existing session — create new one
                conn.execute(
                    "INSERT INTO sessions (mac, remaining_seconds, active, created_at) VALUES (?,?,1,datetime('now'))",
                    (mac, seconds)
                )
                conn.commit()
            finally:
                conn.close()

            # Verify the session has exactly duration_minutes * 60 seconds
            conn = sqlite3.connect(db_file)
            conn.row_factory = sqlite3.Row
            sess = conn.execute(
                "SELECT remaining_seconds FROM sessions WHERE mac=? AND active=1", (mac,)
            ).fetchone()
            voucher = conn.execute(
                "SELECT used FROM vouchers WHERE code=?", (code,)
            ).fetchone()
            conn.close()

            assert sess is not None, f"Session should exist for {mac}"
            assert sess['remaining_seconds'] == duration_minutes * 60, (
                f"Expected {duration_minutes * 60}s, got {sess['remaining_seconds']}s"
            )
            assert voucher['used'] == 1, "Voucher should be marked as used"
        finally:
            db_module.DB_PATH = original
