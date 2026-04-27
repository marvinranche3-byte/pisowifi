#!/usr/bin/env python3
"""
PotsWorks PisoWiFi - Starlink Anti-Throttle Monitor
Monitors WAN throughput every 5 minutes and detects Starlink deprioritization.

How it works:
  1. Every 5 minutes, measure average throughput of active connections
     by reading /proc/net/dev byte counters over a 30-second window.
  2. Compare current throughput to the 30-minute rolling average.
  3. If throughput drops by 50%+ → log throttling event + alert admin panel.
  4. Apply traffic shaping to avoid burst patterns that trigger Starlink
     deprioritization (token bucket smoothing).
"""

import time
import logging
import os
import sys
import subprocess
import json
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
from db import get_db, get_config, set_config

# ── Logging ───────────────────────────────────────────────────────────────────
def _setup_logging():
    handlers = [logging.StreamHandler()]
    log_file = '/var/log/pisowifi-starlink.log'
    try:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        handlers.insert(0, logging.FileHandler(log_file))
    except OSError:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [STARLINK] %(message)s',
        handlers=handlers,
    )

_setup_logging()

# ── Config ────────────────────────────────────────────────────────────────────
MEASURE_INTERVAL   = 300   # 5 minutes between measurements
SAMPLE_DURATION    = 30    # 30-second sampling window
THROTTLE_THRESHOLD = 0.50  # 50% drop triggers alert
HISTORY_WINDOW     = 6     # Keep last 6 measurements (30 minutes) for average
MAX_LOG_ENTRIES    = 720   # Keep ~60 hours of 5-minute logs in DB


def get_wan_interface():
    """Detect the active WAN interface."""
    for iface in ['usb0', 'eth1', 'eth0']:
        if os.path.exists(f'/sys/class/net/{iface}'):
            return iface
    return 'eth0'


def read_rx_bytes(interface):
    """Read received bytes from /proc/net/dev for an interface."""
    try:
        with open('/proc/net/dev') as f:
            for line in f:
                if interface + ':' in line:
                    parts = line.split()
                    # Format: iface: rx_bytes rx_packets ... tx_bytes ...
                    return int(parts[1])
    except Exception:
        pass
    return 0


def measure_throughput_mbps(interface, duration=SAMPLE_DURATION):
    """
    Measure average download throughput over `duration` seconds.

    Returns:
        float: Throughput in Mbps.
    """
    bytes_start = read_rx_bytes(interface)
    time.sleep(duration)
    bytes_end = read_rx_bytes(interface)

    delta_bytes = max(0, bytes_end - bytes_start)
    mbps = (delta_bytes * 8) / (duration * 1_000_000)
    return round(mbps, 3)


def get_recent_measurements(n=HISTORY_WINDOW):
    """Get the last n throughput measurements from the database."""
    conn = get_db()
    try:
        rows = conn.execute(
            'SELECT mbps FROM throughput_log ORDER BY id DESC LIMIT ?', (n,)
        ).fetchall()
        return [row['mbps'] for row in rows]
    finally:
        conn.close()


def save_measurement(mbps, is_throttled=False):
    """Save a throughput measurement to the database."""
    conn = get_db()
    try:
        conn.execute(
            'INSERT INTO throughput_log (timestamp, mbps, is_throttled) VALUES (?,?,?)',
            (datetime.now().isoformat(), mbps, 1 if is_throttled else 0)
        )
        # Prune old entries
        conn.execute(
            'DELETE FROM throughput_log WHERE id NOT IN '
            '(SELECT id FROM throughput_log ORDER BY id DESC LIMIT ?)',
            (MAX_LOG_ENTRIES,)
        )
        conn.commit()
    finally:
        conn.close()


def apply_anti_throttle_shaping(interface, current_mbps):
    """
    Apply token bucket traffic shaping to smooth outbound bursts.
    This reduces the chance of Starlink deprioritization by avoiding
    large burst patterns that trigger their fair-use algorithm.

    Args:
        interface (str): WAN interface.
        current_mbps (float): Current measured throughput.
    """
    # Use 90% of current throughput as the shaping rate to smooth bursts
    shape_mbps = max(1, int(current_mbps * 0.90))
    burst_kb = max(32, shape_mbps * 10)  # burst = 10ms worth of data

    # Remove existing egress shaping
    subprocess.run(
        f'tc qdisc del dev {interface} root 2>/dev/null',
        shell=True, capture_output=True
    )

    # Apply token bucket filter (TBF) for smooth egress
    subprocess.run(
        f'tc qdisc add dev {interface} root tbf '
        f'rate {shape_mbps}mbit burst {burst_kb}kb latency 50ms',
        shell=True, capture_output=True
    )
    logging.info(f'Anti-throttle shaping applied: {shape_mbps}Mbps on {interface}')


def remove_anti_throttle_shaping(interface):
    """Remove traffic shaping from the WAN interface."""
    subprocess.run(
        f'tc qdisc del dev {interface} root 2>/dev/null',
        shell=True, capture_output=True
    )
    logging.info(f'Anti-throttle shaping removed from {interface}')


def check_throttling(current_mbps, history):
    """
    Determine if Starlink throttling is occurring.

    Args:
        current_mbps (float): Current throughput.
        history (list[float]): Recent throughput measurements.

    Returns:
        bool: True if throttling detected.
    """
    if len(history) < 3:
        return False  # Not enough history to compare

    avg = sum(history) / len(history)
    if avg < 0.1:
        return False  # Too low to be meaningful

    drop_ratio = (avg - current_mbps) / avg
    return drop_ratio >= THROTTLE_THRESHOLD


def run():
    """Main Starlink monitor loop."""
    logging.info('Starlink Monitor started.')

    # Wait for system to stabilize
    time.sleep(60)

    while True:
        # Check if feature is enabled
        if get_config('starlink_monitor_enabled', '0') != '1':
            time.sleep(60)
            continue

        wan_if = get_wan_interface()
        logging.info(f'Measuring throughput on {wan_if}...')

        try:
            current_mbps = measure_throughput_mbps(wan_if)
            history = get_recent_measurements()
            is_throttled = check_throttling(current_mbps, history)

            save_measurement(current_mbps, is_throttled)

            if is_throttled:
                avg = sum(history) / len(history) if history else 0
                logging.warning(
                    f'THROTTLING DETECTED! Current: {current_mbps:.2f} Mbps, '
                    f'30-min avg: {avg:.2f} Mbps '
                    f'(drop: {((avg-current_mbps)/avg*100):.0f}%)'
                )
                # Apply anti-throttle shaping
                apply_anti_throttle_shaping(wan_if, current_mbps)
            else:
                logging.info(f'Throughput: {current_mbps:.2f} Mbps — OK')
                # Remove shaping if no longer throttled
                remove_anti_throttle_shaping(wan_if)

        except Exception as e:
            logging.error(f'Measurement error: {e}')

        # Wait for next measurement cycle
        time.sleep(MEASURE_INTERVAL - SAMPLE_DURATION)


if __name__ == '__main__':
    run()
