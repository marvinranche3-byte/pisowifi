"""
Pytest fixtures for PotsWorks PisoWiFi tests.
"""

import os
import sqlite3
import pytest
from unittest.mock import patch


# ── In-memory SQLite fixture ──────────────────────────────────────────────────

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """
    Create a temporary SQLite database with the full PisoWiFi schema and
    patch DB_PATH in the db module so all helpers use it.

    Yields the path to the temporary database file.
    """
    db_file = str(tmp_path / "pisowifi_test.db")

    # Patch DB_PATH in the db module before importing coin_daemon
    import sys
    # Ensure backend is importable
    backend_path = os.path.join(
        os.path.dirname(__file__), '..', 'overlay', 'opt', 'pisowifi', 'backend'
    )
    backend_path = os.path.abspath(backend_path)
    if backend_path not in sys.path:
        sys.path.insert(0, backend_path)

    import db as db_module
    monkeypatch.setattr(db_module, 'DB_PATH', db_file)

    # Initialize schema
    db_module.init_db()

    yield db_file


# ── Mock iptables fixture ─────────────────────────────────────────────────────

@pytest.fixture
def mock_iptables():
    """
    Patch os.system so that iptables commands are no-ops during tests.
    """
    with patch('os.system', return_value=0) as mock_sys:
        yield mock_sys
