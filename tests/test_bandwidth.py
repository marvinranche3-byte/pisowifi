"""
Property-based tests for the PotsWorks PisoWiFi Bandwidth Management.

Covers:
  - Property 8: Per-User Bandwidth Override  (Validates: Requirements 18.2)
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

# Valid MAC address strategy
_mac_strategy = st.from_regex(
    r'([0-9A-F]{2}:){5}[0-9A-F]{2}', fullmatch=True
)


# ===========================================================================
# Property 8: Per-User Bandwidth Override
# Validates: Requirements 18.2
# ===========================================================================

@given(
    _mac_strategy,
    st.integers(min_value=128, max_value=102400),
    st.integers(min_value=128, max_value=102400),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_per_user_bandwidth_override(mac, upload_kbps, download_kbps):
    """
    **Validates: Requirements 18.2**

    Property 8: For any MAC address with a per-user bandwidth rule configured,
    get_bandwidth_rule() must return the per-user rule instead of the global default.
    """
    import db as db_module
    from bandwidth import get_bandwidth_rule

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_file = os.path.join(tmp_dir, "pisowifi_test.db")
        original = db_module.DB_PATH
        db_module.DB_PATH = db_file
        try:
            db_module.init_db()

            # Set global defaults that differ from per-user values
            db_module.set_config('default_upload_kbps', '512')
            db_module.set_config('default_download_kbps', '2048')

            # Insert per-user bandwidth rule
            conn = sqlite3.connect(db_file)
            conn.execute(
                'INSERT INTO bandwidth_rules (mac, upload_kbps, download_kbps, created_at) '
                'VALUES (?, ?, ?, datetime("now"))',
                (mac.upper(), upload_kbps, download_kbps)
            )
            conn.commit()
            conn.close()

            # get_bandwidth_rule should return the per-user values, not global defaults
            rule = get_bandwidth_rule(mac)

            assert rule['upload_kbps'] == upload_kbps, (
                f"Expected per-user upload={upload_kbps}, got {rule['upload_kbps']}"
            )
            assert rule['download_kbps'] == download_kbps, (
                f"Expected per-user download={download_kbps}, got {rule['download_kbps']}"
            )
        finally:
            db_module.DB_PATH = original


@given(_mac_strategy)
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_global_default_used_when_no_per_user_rule(mac):
    """
    When no per-user rule exists, get_bandwidth_rule() must return global defaults.
    """
    import db as db_module
    from bandwidth import get_bandwidth_rule

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_file = os.path.join(tmp_dir, "pisowifi_test.db")
        original = db_module.DB_PATH
        db_module.DB_PATH = db_file
        try:
            db_module.init_db()

            # Set specific global defaults
            db_module.set_config('default_upload_kbps', '1024')
            db_module.set_config('default_download_kbps', '5120')

            # No per-user rule inserted
            rule = get_bandwidth_rule(mac)

            assert rule['upload_kbps'] == 1024, (
                f"Expected global default upload=1024, got {rule['upload_kbps']}"
            )
            assert rule['download_kbps'] == 5120, (
                f"Expected global default download=5120, got {rule['download_kbps']}"
            )
        finally:
            db_module.DB_PATH = original
