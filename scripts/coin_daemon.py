#!/usr/bin/env python3
"""
PotsWorks PisoWiFi - Coin Slot Daemon
Orange Pi 1 - Physical Pin 3 = PA12 = GPIO 12
Universal coin slot white wire connected to Pin 3
"""

import time
import os
import sys
import sqlite3
import logging
import json
from datetime import datetime

# Add backend to path so we can import db helpers
sys.path.insert(0, '/opt/pisowifi/backend')
from db import get_db, get_config, set_config

# GPIO path is derived from the configured pin at runtime
DB_PATH = "/opt/pisowifi/db/pisowifi.db"

def _setup_logging():
    """Configure logging, falling back to stderr-only if the log dir is missing."""
    handlers = [logging.StreamHandler()]
    log_file = '/var/log/pisowifi-coin.log'
    try:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        handlers.insert(0, logging.FileHandler(log_file))
    except OSError:
        pass  # Running outside the target device (e.g., tests on Windows)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [COIN] %(message)s',
        handlers=handlers,
    )


_setup_logging()


# ── Config helpers ────────────────────────────────────────────────────────────

def get_gpio_pin():
    """Read GPIO pin number from config table. Default: 12 (PA12, Physical Pin 3)."""
    return int(get_config('gpio_pin', '12'))


def get_debounce_ms():
    """Read debounce window in milliseconds from config. Default: 50ms."""
    return int(get_config('coin_debounce_ms', '50'))


def get_pulse_timeout():
    """Read pulse accumulation timeout from config. Default: 800ms → 0.8s."""
    return float(get_config('coin_pulse_timeout_ms', '800')) / 1000.0


# ── Pure calculation functions ────────────────────────────────────────────────

def calculate_credits(pulse_count, minutes_per_piso):
    """
    Pure function: convert pulse count to credit seconds.

    Args:
        pulse_count (int): Number of coin pulses received.
        minutes_per_piso (int): Minutes of internet time per 1 piso.

    Returns:
        int: Total credit in seconds.
    """
    return pulse_count * minutes_per_piso * 60


def get_coin_rates():
    """
    Read coin_rates JSON from config table.

    Returns:
        list[dict]: List of {piso, minutes} dicts.
                    Falls back to [{piso: 1, minutes: 5}] if not configured.
    """
    raw = get_config('coin_rates', None)
    if raw:
        try:
            rates = json.loads(raw)
            if isinstance(rates, list) and len(rates) > 0:
                return rates
        except (json.JSONDecodeError, ValueError):
            pass
    return [{"piso": 1, "minutes": 5}]


def pulses_to_minutes(pulse_count):
    """
    Convert a pulse count to total minutes using the configured coin rates.

    The coin rates table maps piso amounts to minutes. Since 1 pulse = 1 piso
    by default, we look up the total piso value in the rates table.

    For multi-denomination coins, the pulse_count represents the total piso
    value (e.g., a P5 coin sends 5 pulses = 5 piso).

    Args:
        pulse_count (int): Number of pulses (= piso value).

    Returns:
        int: Total minutes of internet time to credit.
    """
    rates = get_coin_rates()

    # Build a lookup dict: piso -> minutes
    rate_map = {r['piso']: r['minutes'] for r in rates}

    # Try exact match first (e.g., 5 pulses = P5 coin = 30 minutes)
    if pulse_count in rate_map:
        return rate_map[pulse_count]

    # Fall back to linear calculation using the 1-piso rate
    minutes_per_piso = rate_map.get(1, 5)
    total_seconds = calculate_credits(pulse_count, minutes_per_piso)
    return total_seconds // 60


# ── Orphan credits ────────────────────────────────────────────────────────────

def store_orphan_credits(pulse_count):
    """
    Store unassigned coin pulses as orphan credits in the config table.

    Orphan credits occur when a coin is inserted but no client is currently
    pending. They are applied to the next client that connects within 5 minutes.

    Args:
        pulse_count (int): Number of unassigned pulses to store.
    """
    set_config('orphan_credits', str(pulse_count))
    set_config('orphan_credits_time', str(time.time()))
    logging.info(
        f"Stored {pulse_count} orphan pulse(s) — waiting for next client "
        f"(expires in 5 minutes)"
    )


def apply_orphan_credits(mac):
    """
    Apply any stored orphan credits to the given MAC address if they are
    still within the 5-minute validity window.

    Args:
        mac (str): MAC address of the newly connected client.

    Returns:
        bool: True if orphan credits were applied, False otherwise.
    """
    try:
        orphan_count = int(get_config('orphan_credits', '0'))
        orphan_time = float(get_config('orphan_credits_time', '0'))
    except (ValueError, TypeError):
        return False

    if orphan_count <= 0:
        return False

    now = time.time()
    if (now - orphan_time) >= 300:  # 5-minute window
        logging.info(
            f"Orphan credits ({orphan_count} pulses) expired — discarding"
        )
        set_config('orphan_credits', '0')
        set_config('orphan_credits_time', '0')
        return False

    # Apply the orphan credits to this client
    logging.info(
        f"Applying {orphan_count} orphan pulse(s) to {mac}"
    )
    add_credits(mac, orphan_count)

    # Reset orphan credits
    set_config('orphan_credits', '0')
    set_config('orphan_credits_time', '0')
    logging.info(f"Orphan credits applied to {mac} and reset to 0")
    return True


# ── GPIO helpers ──────────────────────────────────────────────────────────────

def export_gpio():
    """Export and configure the GPIO pin for coin slot input."""
    gpio_pin = get_gpio_pin()
    gpio_path = f"/sys/class/gpio/gpio{gpio_pin}"

    if not os.path.exists(gpio_path):
        with open("/sys/class/gpio/export", "w") as f:
            f.write(str(gpio_pin))
        time.sleep(0.1)

    with open(f"{gpio_path}/direction", "w") as f:
        f.write("in")
    # Enable pull-up via edge detection
    with open(f"{gpio_path}/edge", "w") as f:
        f.write("falling")

    logging.info(f"GPIO {gpio_pin} (Pin 3) initialized for coin slot")
    return gpio_pin


def read_gpio(gpio_pin):
    """Read the current value of the GPIO pin."""
    gpio_path = f"/sys/class/gpio/gpio{gpio_pin}"
    with open(f"{gpio_path}/value", "r") as f:
        return int(f.read().strip())


# ── Database helpers ──────────────────────────────────────────────────────────

def add_credits(mac_address, pulses):
    """
    Add coin credits to a session for the given MAC address.

    Uses pulses_to_minutes() for the pulse-to-time conversion, logs the
    event to /var/log/pisowifi-coin.log, and inserts a transaction record.

    Args:
        mac_address (str): Client MAC address.
        pulses (int): Number of coin pulses to credit.
    """
    minutes = pulses_to_minutes(pulses)
    seconds = minutes * 60
    now_iso = datetime.now().isoformat()

    conn = get_db()
    cur = conn.cursor()
    try:
        # Check if active session exists
        cur.execute(
            "SELECT id, remaining_seconds FROM sessions WHERE mac=? AND active=1",
            (mac_address,)
        )
        row = cur.fetchone()

        if row:
            new_seconds = row['remaining_seconds'] + seconds
            cur.execute(
                "UPDATE sessions SET remaining_seconds=? WHERE id=?",
                (new_seconds, row['id'])
            )
            logging.info(
                f"Added {minutes} min to {mac_address} | "
                f"Total: {new_seconds // 60} min | "
                f"Timestamp: {now_iso}"
            )
        else:
            cur.execute(
                """
                INSERT INTO sessions (mac, remaining_seconds, active, created_at)
                VALUES (?, ?, 1, ?)
                """,
                (mac_address, seconds, now_iso)
            )
            logging.info(
                f"New session for {mac_address} | {minutes} min | "
                f"Timestamp: {now_iso}"
            )

        # Insert transaction record
        cur.execute(
            """
            INSERT INTO transactions (mac, type, amount_piso, minutes, created_at)
            VALUES (?, 'coin', ?, ?, ?)
            """,
            (mac_address, pulses, minutes, now_iso)
        )

        conn.commit()
    finally:
        conn.close()


def get_last_connected_mac():
    """
    Get the most recently connected pending client MAC address.
    Applies any stored orphan credits to the client before returning.

    Returns:
        str | None: MAC address string, or None if no pending client.
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT mac FROM pending_clients ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    conn.close()

    if row is None:
        return None

    mac = row['mac']

    # Apply any pending orphan credits to this client
    apply_orphan_credits(mac)

    return mac


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    gpio_pin = export_gpio()
    logging.info("Coin daemon started. Waiting for coins...")

    last_state = 1  # Pull-up = idle HIGH
    pulse_count = 0
    last_pulse_time = 0

    # Read initial config values
    debounce_ms = get_debounce_ms()
    pulse_timeout = get_pulse_timeout()

    # Track when we last refreshed config
    last_config_refresh = time.time()
    CONFIG_REFRESH_INTERVAL = 60  # seconds

    while True:
        now = time.time()

        # Periodically re-read config to pick up changes without restart
        if (now - last_config_refresh) >= CONFIG_REFRESH_INTERVAL:
            new_pin = get_gpio_pin()
            if new_pin != gpio_pin:
                logging.info(
                    f"GPIO pin changed from {gpio_pin} to {new_pin} — re-initializing"
                )
                gpio_pin = export_gpio()
            debounce_ms = get_debounce_ms()
            pulse_timeout = get_pulse_timeout()
            last_config_refresh = now

        current_state = read_gpio(gpio_pin)

        # Detect falling edge (HIGH -> LOW = pulse)
        if last_state == 1 and current_state == 0:
            if (now - last_pulse_time) > (debounce_ms / 1000.0):
                pulse_count += 1
                last_pulse_time = now
                logging.info(f"Pulse detected! Count: {pulse_count}")

        # If no pulse for pulse_timeout, process the accumulated coins
        if pulse_count > 0 and (now - last_pulse_time) > pulse_timeout:
            mac = get_last_connected_mac()
            if mac:
                add_credits(mac, pulse_count)
            else:
                logging.warning(
                    f"{pulse_count} pulse(s) received but no client connected — "
                    f"storing as orphan credits"
                )
                store_orphan_credits(pulse_count)
            pulse_count = 0

        last_state = current_state
        time.sleep(0.01)  # 10ms polling


if __name__ == "__main__":
    main()
