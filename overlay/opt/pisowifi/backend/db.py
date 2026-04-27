#!/usr/bin/env python3
"""
PotsWorks PisoWiFi - Centralized Database Helper
Provides get_db(), get_config(), set_config(), and init_db() for all modules.
"""

import sqlite3
import hashlib

# Path to the SQLite database file
DB_PATH = "/opt/pisowifi/db/pisowifi.db"

# SHA-256 hash of the default admin password "potsworks2024"
_DEFAULT_ADMIN_PASSWORD_HASH = hashlib.sha256(b"potsworks2024").hexdigest()

# Default coin rates: list of {piso, minutes} objects stored as JSON
_DEFAULT_COIN_RATES = (
    '[{"piso": 1, "minutes": 5}, {"piso": 5, "minutes": 30}, '
    '{"piso": 10, "minutes": 60}, {"piso": 20, "minutes": 150}]'
)

# Default config values seeded on first init
_DEFAULT_CONFIG = [
    ("ssid",                     "PotsWorks PisoWifi"),
    ("wifi_password",            "potsworks123"),
    ("wifi_channel",             "6"),
    ("wifi_band",                "2.4"),
    ("rate_piso_per_minute",     "5"),
    ("coin_pulse_timeout_ms",    "800"),
    ("coin_debounce_ms",         "50"),
    ("gpio_pin",                 "12"),
    ("default_upload_kbps",      "1024"),
    ("default_download_kbps",    "5120"),
    ("qos_enabled",              "0"),
    ("starlink_monitor_enabled", "0"),
    ("coin_sound_enabled",       "1"),
    ("activation_status",        "trial"),
    ("admin_password_hash",      _DEFAULT_ADMIN_PASSWORD_HASH),
    ("coin_rates",               _DEFAULT_COIN_RATES),
    ("orphan_credits",           "0"),
    ("orphan_credits_time",      "0"),
    ("banner_path",              ""),
    ("custom_sound_path",        ""),
    ("vlan_id",                  "0"),
    # Network interface roles:
    # wan_interface_type: "builtin" = eth0 is WAN (default), "usb" = USB-to-LAN is WAN
    ("wan_interface_type",       "builtin"),
    ("detected_setup",           "unknown"),
    ("detected_wan_if",          "eth0"),
]


def get_db():
    """
    Open and return a SQLite connection to the PisoWiFi database.
    Uses sqlite3.Row as row_factory so rows can be accessed like dicts.
    Applies performance pragmas on every connection.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Performance pragmas — applied on every connection
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=2000")
    conn.execute("PRAGMA temp_store=MEMORY")

    return conn


def get_config(key, default=None):
    """
    Read a single value from the config table.

    Args:
        key (str): Config key to look up.
        default: Value to return if the key does not exist.

    Returns:
        The stored string value, or `default` if not found.
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT value FROM config WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def set_config(key, value):
    """
    Write (insert or replace) a value in the config table.

    Args:
        key (str): Config key.
        value (str): Value to store (always stored as text).
    """
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            (key, str(value))
        )
        conn.commit()
    finally:
        conn.close()


def init_db():
    """
    Create all 8 database tables (if they don't exist), add indexes,
    and seed default config values.

    Safe to call multiple times — uses CREATE TABLE IF NOT EXISTS and
    INSERT OR IGNORE for config seeding.
    """
    conn = get_db()
    cur = conn.cursor()

    # ── Table: sessions ──────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            mac              TEXT    NOT NULL,
            remaining_seconds INTEGER DEFAULT 0,
            active           INTEGER DEFAULT 0,
            created_at       TEXT,
            bandwidth_tier   TEXT    DEFAULT NULL
        )
    """)

    # ── Table: pending_clients ───────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pending_clients (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            mac          TEXT,
            ip           TEXT,
            connected_at TEXT
        )
    """)

    # ── Table: vouchers ──────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vouchers (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            code           TEXT    UNIQUE NOT NULL,
            minutes        INTEGER NOT NULL,
            used           INTEGER DEFAULT 0,
            used_by        TEXT,
            used_at        TEXT,
            created_at     TEXT,
            bandwidth_tier TEXT    DEFAULT NULL,
            prefix         TEXT    DEFAULT NULL
        )
    """)

    # ── Table: transactions ──────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            mac         TEXT,
            type        TEXT,
            amount_piso REAL,
            minutes     INTEGER,
            created_at  TEXT
        )
    """)

    # ── Table: bandwidth_rules ───────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bandwidth_rules (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            mac           TEXT    UNIQUE NOT NULL,
            upload_kbps   INTEGER,
            download_kbps INTEGER,
            created_at    TEXT,
            notes         TEXT
        )
    """)

    # ── Table: config ────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # ── Table: activation ────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS activation (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            license_key_hash TEXT,
            hardware_id      TEXT,
            activated_at     TEXT,
            status           TEXT
        )
    """)

    # ── Table: throughput_log ────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS throughput_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp    TEXT,
            mbps         REAL,
            is_throttled INTEGER DEFAULT 0
        )
    """)

    # ── Indexes ──────────────────────────────────────────────────────
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_mac    ON sessions(mac)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_active ON sessions(active)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_vouchers_code   ON vouchers(code)"
    )

    # ── Seed default config values (INSERT OR IGNORE — never overwrite) ──
    cur.executemany(
        "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
        _DEFAULT_CONFIG
    )

    conn.commit()
    conn.close()
