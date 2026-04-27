#!/usr/bin/env python3
"""
PotsWorks PisoWiFi - License Key Activation System
Offline HMAC-SHA256 validation tied to device hardware ID.

The license key format: XXXXX-XXXXX-XXXXX-XXXXX (20 hex chars in 4 groups of 5)
The hardware ID format: OPI-AABBCCDDEEFF (prefix + MAC address without colons)

Validation: HMAC-SHA256(hardware_id, SECRET_SALT) → truncate to 20 hex chars → format as key
"""

import hashlib
import hmac
import subprocess
import os
import sys
import logging
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
from db import get_db, get_config, set_config

logger = logging.getLogger(__name__)

# Internal salt used for HMAC key generation.
# This is baked into the firmware — changing it invalidates all existing keys.
_HMAC_SALT = b'PotsWorks-PisoWifi-2026-License-Salt'


def get_hardware_id():
    """
    Get the unique hardware ID for this device.
    Uses the MAC address of the primary network interface.

    Returns:
        str: Hardware ID in format "OPI-AABBCCDDEEFF", or "OPI-000000000000" on error.
    """
    # Try to read MAC from common interfaces in priority order
    interfaces = ['eth0', 'wlan0', 'usb0', 'enp0s3']

    for iface in interfaces:
        mac_path = f'/sys/class/net/{iface}/address'
        try:
            with open(mac_path) as f:
                mac = f.read().strip().replace(':', '').upper()
                if mac and mac != '000000000000':
                    return f'OPI-{mac}'
        except Exception:
            pass

    # Fallback: use ip link show
    try:
        out = subprocess.check_output(
            ['ip', 'link', 'show'], timeout=3, stderr=subprocess.DEVNULL
        ).decode()
        for line in out.splitlines():
            if 'link/ether' in line:
                parts = line.strip().split()
                if len(parts) >= 2:
                    mac = parts[1].replace(':', '').upper()
                    if mac and mac != '000000000000':
                        return f'OPI-{mac}'
    except Exception:
        pass

    return 'OPI-000000000000'


def generate_license_key(hardware_id):
    """
    Generate a valid license key for a given hardware ID.
    Used by the license key issuing system (not on the device itself).

    Args:
        hardware_id (str): Hardware ID in format "OPI-AABBCCDDEEFF".

    Returns:
        str: License key in format "XXXXX-XXXXX-XXXXX-XXXXX".
    """
    digest = hmac.new(
        _HMAC_SALT,
        hardware_id.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    # Take first 20 hex chars and format as 4 groups of 5
    key_raw = digest[:20].upper()
    return '-'.join([key_raw[i:i+5] for i in range(0, 20, 5)])


def validate_license_key(key, hardware_id=None):
    """
    Validate a license key against the device's hardware ID.
    Fully offline — no network required.

    Args:
        key (str): License key in format "XXXXX-XXXXX-XXXXX-XXXXX".
        hardware_id (str|None): Hardware ID to validate against.
                                Uses get_hardware_id() if None.

    Returns:
        bool: True if the key is valid for this hardware, False otherwise.
    """
    if hardware_id is None:
        hardware_id = get_hardware_id()

    # Normalize key: remove dashes, uppercase
    key_normalized = key.replace('-', '').upper().strip()
    if len(key_normalized) != 20:
        return False

    # Generate expected key for this hardware ID
    expected = generate_license_key(hardware_id).replace('-', '').upper()

    # Constant-time comparison to prevent timing attacks
    return hmac.compare_digest(key_normalized, expected)


def activate(key):
    """
    Validate and activate the system with a license key.

    Args:
        key (str): License key entered by the operator.

    Returns:
        dict: {'success': bool, 'message': str}
    """
    hardware_id = get_hardware_id()

    if not validate_license_key(key, hardware_id):
        logger.warning(f'Invalid license key attempt for hardware {hardware_id}')
        return {
            'success': False,
            'message': 'Hindi valid ang license key para sa device na ito.'
        }

    # Store activation record
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    now = datetime.now().isoformat()

    conn = get_db()
    try:
        conn.execute(
            'INSERT INTO activation (license_key_hash, hardware_id, activated_at, status) '
            'VALUES (?, ?, ?, ?)',
            (key_hash, hardware_id, now, 'activated')
        )
        conn.commit()
    finally:
        conn.close()

    # Update config
    set_config('activation_status', 'activated')

    logger.info(f'System activated successfully for hardware {hardware_id}')
    return {
        'success': True,
        'message': 'Sistema ay matagumpay na na-activate!'
    }


def get_activation_status():
    """
    Get the current activation status.

    Returns:
        dict: {
            'status': 'trial' | 'activated',
            'activated_at': str | None,
            'masked_key': str | None,
            'hardware_id': str
        }
    """
    hardware_id = get_hardware_id()
    status = get_config('activation_status', 'trial')

    conn = get_db()
    try:
        row = conn.execute(
            'SELECT license_key_hash, activated_at FROM activation '
            'WHERE status=? ORDER BY id DESC LIMIT 1',
            ('activated',)
        ).fetchone()
    finally:
        conn.close()

    if row:
        key_hash = row['license_key_hash'] or ''
        # Show last 4 chars of hash as masked key indicator
        masked = 'XXXXX-XXXXX-XXXXX-' + key_hash[-4:].upper() if len(key_hash) >= 4 else '****'
        return {
            'status': status,
            'activated_at': row['activated_at'],
            'masked_key': masked,
            'hardware_id': hardware_id,
        }

    return {
        'status': status,
        'activated_at': None,
        'masked_key': None,
        'hardware_id': hardware_id,
    }


def is_activated():
    """Quick check: returns True if system is fully activated."""
    return get_config('activation_status', 'trial') == 'activated'


def get_max_users():
    """Returns max simultaneous users based on activation status."""
    return 999999 if is_activated() else 2
