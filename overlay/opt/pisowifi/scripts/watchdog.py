#!/usr/bin/env python3
"""
PotsWorks PisoWiFi - Watchdog Service
Monitors all PisoWifi services and restarts them if they stop responding.

Monitored services:
  - pisowifi-backend  (Flask) — HTTP health check on /api/session_status
  - pisowifi-coin     (Coin Daemon) — systemctl is-active check
  - pisowifi-session  (Session Manager) — systemctl is-active check
"""

import subprocess
import time
import logging
import os
import sys

# ── Logging ───────────────────────────────────────────────────────────────────
def _setup_logging():
    handlers = [logging.StreamHandler()]
    log_file = '/var/log/pisowifi-watchdog.log'
    try:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        handlers.insert(0, logging.FileHandler(log_file))
    except OSError:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [WATCHDOG] %(message)s',
        handlers=handlers,
    )

_setup_logging()

# ── Config ────────────────────────────────────────────────────────────────────
CHECK_INTERVAL   = 5    # seconds between checks
HTTP_TIMEOUT     = 5    # seconds for Flask health check
RESTART_COOLDOWN = 30   # seconds to wait after a restart before checking again

SERVICES = [
    {
        'name': 'pisowifi-backend',
        'check': 'http',
        'url': 'http://127.0.0.1/api/session_status',
    },
    {
        'name': 'pisowifi-coin',
        'check': 'systemctl',
    },
    {
        'name': 'pisowifi-session',
        'check': 'systemctl',
    },
]

# Track last restart time per service to avoid restart loops
_last_restart = {}


def check_http(url, timeout=HTTP_TIMEOUT):
    """Return True if the HTTP endpoint responds with 2xx."""
    try:
        import urllib.request
        req = urllib.request.urlopen(url, timeout=timeout)
        return 200 <= req.status < 300
    except Exception:
        return False


def check_systemctl(service_name):
    """Return True if the systemd service is active."""
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', service_name],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() == 'active'
    except Exception:
        return False


def restart_service(service_name):
    """Restart a systemd service and log the event."""
    now = time.time()
    last = _last_restart.get(service_name, 0)

    if (now - last) < RESTART_COOLDOWN:
        logging.warning(
            f'{service_name}: restart skipped (cooldown {RESTART_COOLDOWN}s not elapsed)'
        )
        return

    logging.warning(f'{service_name}: not responding — restarting...')
    try:
        subprocess.run(
            ['systemctl', 'restart', service_name],
            timeout=15, check=False
        )
        _last_restart[service_name] = time.time()
        logging.info(f'{service_name}: restart command sent')
    except Exception as e:
        logging.error(f'{service_name}: restart failed — {e}')


def run():
    """Main watchdog loop."""
    logging.info('Watchdog started. Monitoring: ' +
                 ', '.join(s['name'] for s in SERVICES))

    # Give services time to start up before first check
    time.sleep(15)

    while True:
        for svc in SERVICES:
            name = svc['name']
            try:
                if svc['check'] == 'http':
                    ok = check_http(svc['url'])
                else:
                    ok = check_systemctl(name)

                if not ok:
                    restart_service(name)
            except Exception as e:
                logging.error(f'{name}: check error — {e}')

        time.sleep(CHECK_INTERVAL)


if __name__ == '__main__':
    run()
