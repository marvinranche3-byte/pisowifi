#!/usr/bin/env python3
"""
PotsWorks PisoWiFi - Bandwidth Management Module
Implements per-user bandwidth limiting using Linux tc/HTB.

Usage:
    from bandwidth import apply_bandwidth_rule, remove_bandwidth_rule, setup_root_qdisc
"""

import os
import subprocess
import logging
import sys

sys.path.insert(0, os.path.dirname(__file__))
from db import get_db, get_config

logger = logging.getLogger(__name__)


def _run(cmd):
    """Run a shell command, return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=5
        )
        return result.returncode, result.stdout, result.stderr
    except Exception as e:
        logger.error(f"Command failed: {cmd} — {e}")
        return 1, '', str(e)


def setup_root_qdisc(interface='wlan0'):
    """
    Set up the root HTB qdisc on the given interface.
    Safe to call multiple times — removes existing qdisc first.

    Args:
        interface (str): Network interface name (default: wlan0).
    """
    # Remove existing root qdisc (ignore errors if none exists)
    _run(f'tc qdisc del dev {interface} root 2>/dev/null')

    # Add HTB root qdisc with default class 999 (unlimited)
    rc, _, err = _run(f'tc qdisc add dev {interface} root handle 1: htb default 999')
    if rc != 0:
        logger.error(f'Failed to add root qdisc on {interface}: {err}')
        return False

    # Add default unlimited class
    _run(f'tc class add dev {interface} parent 1: classid 1:999 htb rate 1000mbit')
    logger.info(f'Root HTB qdisc set up on {interface}')
    return True


def get_mark_id(mac):
    """
    Get a unique integer mark ID for a MAC address.
    Uses the session ID from the database as the mark.

    Args:
        mac (str): MAC address.

    Returns:
        int: Mark ID (1–65535), or 100 as fallback.
    """
    conn = get_db()
    try:
        row = conn.execute(
            'SELECT id FROM sessions WHERE mac=? AND active=1 ORDER BY id DESC LIMIT 1',
            (mac,)
        ).fetchone()
        if row:
            # Keep mark ID in valid range (1–65535)
            return (row['id'] % 65534) + 1
    finally:
        conn.close()
    return 100


def apply_bandwidth_rule(mac, upload_kbps=None, download_kbps=None,
                          mark_id=None, interface='wlan0'):
    """
    Apply per-user bandwidth limiting for a MAC address using tc/HTB.

    If upload_kbps/download_kbps are not provided, reads from:
    1. bandwidth_rules table (per-user override)
    2. config table (global default)

    Args:
        mac (str): Client MAC address.
        upload_kbps (int|None): Upload limit in kbps.
        download_kbps (int|None): Download limit in kbps.
        mark_id (int|None): iptables mark ID. Auto-assigned if None.
        interface (str): Network interface.
    """
    mac = mac.upper()

    # Resolve limits
    if upload_kbps is None or download_kbps is None:
        conn = get_db()
        try:
            row = conn.execute(
                'SELECT upload_kbps, download_kbps FROM bandwidth_rules WHERE mac=?',
                (mac,)
            ).fetchone()
            if row:
                upload_kbps = upload_kbps or row['upload_kbps']
                download_kbps = download_kbps or row['download_kbps']
        finally:
            conn.close()

    if upload_kbps is None:
        upload_kbps = int(get_config('default_upload_kbps', '1024') or 1024)
    if download_kbps is None:
        download_kbps = int(get_config('default_download_kbps', '5120') or 5120)

    if mark_id is None:
        mark_id = get_mark_id(mac)

    # Create HTB class for this user
    class_id = f'1:{mark_id}'
    _run(f'tc class add dev {interface} parent 1: classid {class_id} '
         f'htb rate {download_kbps}kbit ceil {download_kbps}kbit')

    # Add filter: match iptables mark → HTB class
    _run(f'tc filter add dev {interface} parent 1: protocol ip '
         f'handle {mark_id} fw flowid {class_id}')

    # Mark outgoing packets for this MAC with iptables
    _run(f'iptables -t mangle -A POSTROUTING -m mac --mac-source {mac} '
         f'-j MARK --set-mark {mark_id}')

    logger.info(
        f'Bandwidth rule applied: {mac} → '
        f'up={upload_kbps}kbps down={download_kbps}kbps mark={mark_id}'
    )
    return mark_id


def remove_bandwidth_rule(mac, mark_id=None, interface='wlan0'):
    """
    Remove per-user bandwidth limiting for a MAC address.

    Args:
        mac (str): Client MAC address.
        mark_id (int|None): iptables mark ID. Auto-resolved if None.
        interface (str): Network interface.
    """
    mac = mac.upper()

    if mark_id is None:
        mark_id = get_mark_id(mac)

    class_id = f'1:{mark_id}'

    # Remove tc filter
    _run(f'tc filter del dev {interface} parent 1: handle {mark_id} fw 2>/dev/null')

    # Remove tc class
    _run(f'tc class del dev {interface} classid {class_id} 2>/dev/null')

    # Remove iptables mangle rule
    _run(f'iptables -t mangle -D POSTROUTING -m mac --mac-source {mac} '
         f'-j MARK --set-mark {mark_id} 2>/dev/null')

    logger.info(f'Bandwidth rule removed: {mac} (mark={mark_id})')


def get_bandwidth_rule(mac):
    """
    Get the configured bandwidth rule for a MAC address.

    Returns:
        dict with upload_kbps and download_kbps, or global defaults.
    """
    mac = mac.upper()
    conn = get_db()
    try:
        row = conn.execute(
            'SELECT upload_kbps, download_kbps FROM bandwidth_rules WHERE mac=?',
            (mac,)
        ).fetchone()
        if row:
            return {'upload_kbps': row['upload_kbps'], 'download_kbps': row['download_kbps']}
    finally:
        conn.close()

    return {
        'upload_kbps': int(get_config('default_upload_kbps', '1024') or 1024),
        'download_kbps': int(get_config('default_download_kbps', '5120') or 5120),
    }
