#!/usr/bin/env python3
"""
PotsWorks PisoWiFi - Session Manager
Runs every second, decrements time, blocks expired sessions.
On startup, restores iptables rules for all active sessions (reboot recovery).
"""

import os
import sys
import time
import logging
from datetime import datetime

# Add backend to path so we can import db helpers
sys.path.insert(0, '/opt/pisowifi/backend')
from db import get_db, get_config


def _setup_logging():
    """Configure logging, falling back to stderr-only if the log dir is missing."""
    handlers = [logging.StreamHandler()]
    log_file = '/var/log/pisowifi-session.log'
    try:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        handlers.insert(0, logging.FileHandler(log_file))
    except OSError:
        pass  # Running outside the target device (e.g., tests on Windows/dev)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [SESSION] %(message)s',
        handlers=handlers,
    )


_setup_logging()


def allow_mac(mac):
    """
    Allow internet access for a MAC address via iptables.
    Inserts rules at the top of the chain so they take priority.
    """
    os.system(f"iptables -t nat -I PREROUTING 1 -m mac --mac-source {mac} -j ACCEPT")
    os.system(f"iptables -I FORWARD 1 -m mac --mac-source {mac} -j ACCEPT")


def remove_bandwidth_rule(mac):
    """
    Stub: remove per-user bandwidth (tc/HTB) rule for a MAC address.
    Full implementation is in Task 13 (bandwidth.py).
    """
    logging.info(f"Would remove bandwidth rule for {mac}")


def block_mac(mac):
    """
    Block internet access for a MAC address via iptables.
    Logs the expiration event with MAC and timestamp.
    Also cleans up any bandwidth rule for the session.
    """
    os.system(f"iptables -t nat -D PREROUTING -m mac --mac-source {mac} -j ACCEPT 2>/dev/null")
    os.system(f"iptables -D FORWARD -m mac --mac-source {mac} -j ACCEPT 2>/dev/null")
    remove_bandwidth_rule(mac)
    logging.info(
        f"Session expired: {mac} | Timestamp: {datetime.now().isoformat()}"
    )


def restore_sessions():
    """
    Reboot recovery: re-apply iptables ACCEPT rules for all active sessions.

    Queries all sessions with active=1 and remaining_seconds > 0, then calls
    allow_mac() for each one so that customers who had time remaining before
    a reboot can continue browsing without re-inserting coins.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, mac, remaining_seconds FROM sessions "
            "WHERE active=1 AND remaining_seconds > 0"
        ).fetchall()
    finally:
        conn.close()

    count = len(rows)
    for row in rows:
        allow_mac(row['mac'])

    logging.info(f"Restored {count} active session(s) after startup/reboot")


def run():
    """
    Main session manager loop.

    1. Restores iptables rules for all active sessions (reboot recovery).
    2. Every second, decrements remaining_seconds for all active sessions.
    3. When a session reaches 0, marks it inactive and blocks the MAC.
    """
    restore_sessions()

    while True:
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT id, mac, remaining_seconds FROM sessions WHERE active=1"
            ).fetchall()

            for row in rows:
                sid = row['id']
                mac = row['mac']
                secs = row['remaining_seconds']

                secs -= 1
                if secs <= 0:
                    conn.execute(
                        "UPDATE sessions SET remaining_seconds=0, active=0 WHERE id=?",
                        (sid,)
                    )
                    block_mac(mac)
                else:
                    conn.execute(
                        "UPDATE sessions SET remaining_seconds=? WHERE id=?",
                        (secs, sid)
                    )

            conn.commit()
        finally:
            conn.close()

        time.sleep(1)


if __name__ == "__main__":
    run()
