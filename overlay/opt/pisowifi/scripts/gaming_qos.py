#!/usr/bin/env python3
"""
PotsWorks PisoWiFi - Gaming QoS Module
Implements HTB-based traffic prioritization for gaming traffic.

Gaming traffic (low-latency UDP) gets 80% of WAN bandwidth.
Bulk traffic (HTTP downloads, streaming) gets 20%.

Default gaming ports: 3074, 3478, 3479, 27015-27030
"""

import subprocess
import logging
import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
from db import get_db, get_config, set_config

logger = logging.getLogger(__name__)

# Default gaming ports (UDP)
DEFAULT_GAMING_PORTS = [3074, 3478, 3479] + list(range(27015, 27031))

# Default WAN bandwidth assumption if not configured (Mbps)
DEFAULT_WAN_MBPS = 50


def _run(cmd, check=False):
    """Run a shell command silently."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0 and check:
            logger.warning(f'QoS cmd failed: {cmd} — {result.stderr.strip()}')
        return result.returncode == 0
    except Exception as e:
        logger.error(f'QoS cmd error: {cmd} — {e}')
        return False


def get_gaming_ports():
    """Read gaming ports from config. Returns list of ints."""
    raw = get_config('gaming_ports', None)
    if raw:
        try:
            ports = json.loads(raw)
            if isinstance(ports, list):
                return [int(p) for p in ports]
        except Exception:
            pass
    return DEFAULT_GAMING_PORTS


def set_gaming_ports(ports):
    """Save gaming ports list to config."""
    set_config('gaming_ports', json.dumps([int(p) for p in ports]))


def get_wan_interface():
    """Detect the active WAN interface."""
    for iface in ['usb0', 'eth1', 'eth0']:
        if os.path.exists(f'/sys/class/net/{iface}'):
            return iface
    return 'eth0'


def enable_gaming_qos(wan_interface=None, wan_bandwidth_mbps=None):
    """
    Set up HTB QoS on the WAN interface.

    Traffic classes:
      1:10 — Gaming (high priority, 80% bandwidth, low latency)
      1:20 — Bulk/streaming (low priority, 20% bandwidth)
      1:999 — Default (unclassified traffic → bulk)

    Args:
        wan_interface (str|None): WAN interface. Auto-detected if None.
        wan_bandwidth_mbps (int|None): Total WAN bandwidth. Reads from config if None.
    """
    if wan_interface is None:
        wan_interface = get_wan_interface()
    if wan_bandwidth_mbps is None:
        wan_bandwidth_mbps = int(get_config('wan_bandwidth_mbps', str(DEFAULT_WAN_MBPS)) or DEFAULT_WAN_MBPS)

    gaming_mbps = int(wan_bandwidth_mbps * 0.80)
    bulk_mbps   = int(wan_bandwidth_mbps * 0.20)
    # Ensure minimums
    gaming_mbps = max(gaming_mbps, 1)
    bulk_mbps   = max(bulk_mbps, 1)

    logger.info(f'Enabling Gaming QoS on {wan_interface} '
                f'(total={wan_bandwidth_mbps}Mbps, gaming={gaming_mbps}Mbps, bulk={bulk_mbps}Mbps)')

    # Remove existing root qdisc
    _run(f'tc qdisc del dev {wan_interface} root 2>/dev/null')

    # Root HTB qdisc — default class 999 (bulk)
    _run(f'tc qdisc add dev {wan_interface} root handle 1: htb default 999')

    # Root class — total bandwidth
    _run(f'tc class add dev {wan_interface} parent 1: classid 1:1 '
         f'htb rate {wan_bandwidth_mbps}mbit ceil {wan_bandwidth_mbps}mbit')

    # Gaming class — high priority, 80% bandwidth
    _run(f'tc class add dev {wan_interface} parent 1:1 classid 1:10 '
         f'htb rate {gaming_mbps}mbit ceil {wan_bandwidth_mbps}mbit prio 1')

    # Bulk class — low priority, 20% bandwidth
    _run(f'tc class add dev {wan_interface} parent 1:1 classid 1:20 '
         f'htb rate {bulk_mbps}mbit ceil {wan_bandwidth_mbps}mbit prio 2')

    # Default class (same as bulk)
    _run(f'tc class add dev {wan_interface} parent 1:1 classid 1:999 '
         f'htb rate {bulk_mbps}mbit ceil {wan_bandwidth_mbps}mbit prio 3')

    # Add SFQ (Stochastic Fair Queuing) leaf qdiscs for fairness
    _run(f'tc qdisc add dev {wan_interface} parent 1:10 handle 10: sfq perturb 10')
    _run(f'tc qdisc add dev {wan_interface} parent 1:20 handle 20: sfq perturb 10')

    # Add gaming port filters
    ports = get_gaming_ports()
    for port in ports:
        add_gaming_port_filter(port, 'udp', wan_interface)

    # Save state
    set_config('qos_enabled', '1')
    set_config('qos_wan_interface', wan_interface)
    logger.info(f'Gaming QoS enabled. {len(ports)} gaming port filters applied.')
    return True


def add_gaming_port_filter(port, protocol='udp', wan_interface=None):
    """
    Add a tc filter to classify traffic on a specific port as gaming (high priority).

    Args:
        port (int): Port number.
        protocol (str): 'udp' or 'tcp'.
        wan_interface (str|None): WAN interface.
    """
    if wan_interface is None:
        wan_interface = get_config('qos_wan_interface', get_wan_interface())

    proto_num = '17' if protocol == 'udp' else '6'  # UDP=17, TCP=6

    # Use u32 filter to match destination port
    _run(f'tc filter add dev {wan_interface} parent 1: protocol ip prio 1 '
         f'u32 match ip protocol {proto_num} 0xff '
         f'match ip dport {port} 0xffff flowid 1:10')

    # Also match source port (for return traffic)
    _run(f'tc filter add dev {wan_interface} parent 1: protocol ip prio 1 '
         f'u32 match ip protocol {proto_num} 0xff '
         f'match ip sport {port} 0xffff flowid 1:10')

    logger.debug(f'Gaming filter added: {protocol.upper()} port {port}')


def remove_gaming_port_filter(port, wan_interface=None):
    """Remove gaming filters for a specific port (by flushing and re-adding all others)."""
    if wan_interface is None:
        wan_interface = get_config('qos_wan_interface', get_wan_interface())

    # Get current ports, remove the specified one
    ports = get_gaming_ports()
    if port in ports:
        ports.remove(port)
        set_gaming_ports(ports)

    # Flush all filters and re-add remaining ones
    _run(f'tc filter del dev {wan_interface} parent 1: 2>/dev/null')
    for p in ports:
        add_gaming_port_filter(p, 'udp', wan_interface)

    logger.info(f'Gaming port {port} removed. {len(ports)} ports remaining.')


def disable_gaming_qos(wan_interface=None):
    """Remove all QoS rules from the WAN interface."""
    if wan_interface is None:
        wan_interface = get_config('qos_wan_interface', get_wan_interface())

    _run(f'tc qdisc del dev {wan_interface} root 2>/dev/null')
    set_config('qos_enabled', '0')
    logger.info(f'Gaming QoS disabled on {wan_interface}')
    return True


def get_qos_status():
    """Return current QoS configuration."""
    enabled = get_config('qos_enabled', '0') == '1'
    ports = get_gaming_ports()
    wan_if = get_config('qos_wan_interface', get_wan_interface())
    wan_mbps = int(get_config('wan_bandwidth_mbps', str(DEFAULT_WAN_MBPS)) or DEFAULT_WAN_MBPS)
    return {
        'enabled': enabled,
        'wan_interface': wan_if,
        'wan_bandwidth_mbps': wan_mbps,
        'gaming_ports': ports,
        'gaming_bandwidth_pct': 80,
        'bulk_bandwidth_pct': 20,
    }
