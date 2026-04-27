#!/bin/bash
# PotsWorks PisoWifi — First Boot Initialization Script
# Runs once on first boot via pisowifi-firstboot.service
# Creates marker file /opt/pisowifi/.first_boot_done when complete

set -e

MARKER="/opt/pisowifi/.first_boot_done"
DB_DIR="/opt/pisowifi/db"
DB_PATH="$DB_DIR/pisowifi.db"
BACKEND_DIR="/opt/pisowifi/backend"
SCRIPTS_DIR="/opt/pisowifi/scripts"
LOG_DIR="/var/log"

log() { echo "[first_boot] $(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a /var/log/pisowifi-firstboot.log; }

# ── Guard: skip if already done ───────────────────────────────────────────────
if [ -f "$MARKER" ]; then
    log "First boot already completed. Skipping."
    exit 0
fi

log "=== PotsWorks PisoWifi First Boot ==="

# ── Create required directories ───────────────────────────────────────────────
log "Creating directories..."
mkdir -p "$DB_DIR"
mkdir -p /opt/pisowifi/portal/assets/banner
mkdir -p /opt/pisowifi/portal/assets/sounds
mkdir -p "$LOG_DIR"

# ── Initialize SQLite database ────────────────────────────────────────────────
log "Initializing database..."
python3 -c "
import sys
sys.path.insert(0, '$BACKEND_DIR')
from db import init_db
init_db()
print('Database initialized.')
"

# ── Generate random admin password ───────────────────────────────────────────
log "Generating admin password..."
ADMIN_PASS=$(python3 -c "import secrets; print(secrets.token_urlsafe(12))")
ADMIN_HASH=$(python3 -c "import hashlib; print(hashlib.sha256('$ADMIN_PASS'.encode()).hexdigest())")

python3 -c "
import sys
sys.path.insert(0, '$BACKEND_DIR')
from db import set_config
set_config('admin_password_hash', '$ADMIN_HASH')
print('Admin password hash saved.')
"

# Save plaintext password to a file for first-time access (operator must change it)
echo "PotsWorks PisoWifi Admin Password: $ADMIN_PASS" > /opt/pisowifi/ADMIN_PASSWORD.txt
echo "Access admin panel at: http://10.0.0.1/admin" >> /opt/pisowifi/ADMIN_PASSWORD.txt
echo "IMPORTANT: Change this password after first login!" >> /opt/pisowifi/ADMIN_PASSWORD.txt
chmod 600 /opt/pisowifi/ADMIN_PASSWORD.txt
log "Admin password saved to /opt/pisowifi/ADMIN_PASSWORD.txt"
log "Admin password: $ADMIN_PASS"

# ── Configure static IP for wlan0 ────────────────────────────────────────────
log "Configuring wlan0 static IP (10.0.0.1)..."
ip addr add 10.0.0.1/24 dev wlan0 2>/dev/null || true
ip link set wlan0 up 2>/dev/null || true

# ── Detect and configure WAN interface ───────────────────────────────────────
log "Detecting WAN interface..."
bash "$SCRIPTS_DIR/detect_wan.sh" || log "WAN detection failed (will retry on next boot)"

# ── Enable and start systemd services ────────────────────────────────────────
log "Enabling systemd services..."
systemctl enable hostapd.service 2>/dev/null || true
systemctl enable dnsmasq.service 2>/dev/null || true
systemctl enable pisowifi-backend.service 2>/dev/null || true
systemctl enable pisowifi-coin.service 2>/dev/null || true
systemctl enable pisowifi-session.service 2>/dev/null || true
systemctl enable pisowifi-watchdog.service 2>/dev/null || true

log "Starting services..."
systemctl start hostapd.service 2>/dev/null || log "hostapd start failed"
systemctl start dnsmasq.service 2>/dev/null || log "dnsmasq start failed"
systemctl start pisowifi-backend.service 2>/dev/null || log "backend start failed"
systemctl start pisowifi-coin.service 2>/dev/null || log "coin daemon start failed"
systemctl start pisowifi-session.service 2>/dev/null || log "session manager start failed"
systemctl start pisowifi-watchdog.service 2>/dev/null || log "watchdog start failed"

# ── Create marker file ────────────────────────────────────────────────────────
touch "$MARKER"
log "=== First boot complete! ==="
log "Admin panel: http://10.0.0.1/admin"
log "Admin password: $ADMIN_PASS"
