#!/bin/bash
# PotsWorks PisoWifi — Armbian Image Customization Script
# Called by Armbian build framework after base image creation
# Installs packages, copies overlay files, enables services

set -e

OVERLAY_DIR="/root/overlay"   # Armbian mounts userpatches here
TARGET_DIR="$SDCARD"          # Armbian's target SD card mount point

log() { echo "[customize] $*"; }

log "=== PotsWorks PisoWifi Image Customization ==="

# ── Install required packages ─────────────────────────────────────────────────
log "Installing packages..."
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-flask \
    hostapd \
    dnsmasq \
    iptables \
    iptables-persistent \
    sqlite3 \
    iproute2 \
    wireless-tools \
    wpasupplicant \
    logrotate \
    ntp \
    curl \
    vlan \
    2>&1

# ── Install Python packages ───────────────────────────────────────────────────
log "Installing Python packages..."
pip3 install flask --break-system-packages 2>&1 || \
pip3 install flask 2>&1 || true

# ── Load required kernel modules ─────────────────────────────────────────────
log "Configuring kernel modules..."
cat >> /etc/modules << 'EOF'
ax88179_178a
r8152
cdc_ether
8021q
EOF

# ── Copy overlay files ────────────────────────────────────────────────────────
log "Copying overlay files..."

# Application files
cp -r "$OVERLAY_DIR/opt/pisowifi" /opt/
chmod +x /opt/pisowifi/scripts/*.sh 2>/dev/null || true
chmod +x /opt/pisowifi/scripts/*.py 2>/dev/null || true

# Network config
cp "$OVERLAY_DIR/etc/hostapd/hostapd.conf" /etc/hostapd/hostapd.conf
cp "$OVERLAY_DIR/etc/dnsmasq.conf" /etc/dnsmasq.conf
cp "$OVERLAY_DIR/etc/network/interfaces" /etc/network/interfaces

# Logrotate
cp "$OVERLAY_DIR/etc/logrotate.d/pisowifi" /etc/logrotate.d/pisowifi

# Systemd services
cp "$OVERLAY_DIR/lib/systemd/system/pisowifi-"*.service /lib/systemd/system/

# ── Enable services ───────────────────────────────────────────────────────────
log "Enabling services..."
systemctl enable hostapd.service
systemctl enable dnsmasq.service
systemctl enable pisowifi-firstboot.service
systemctl enable pisowifi-backend.service
systemctl enable pisowifi-coin.service
systemctl enable pisowifi-session.service
systemctl enable pisowifi-watchdog.service

# ── Configure hostapd ─────────────────────────────────────────────────────────
log "Configuring hostapd..."
sed -i 's/#DAEMON_CONF=""/DAEMON_CONF="\/etc\/hostapd\/hostapd.conf"/' \
    /etc/default/hostapd 2>/dev/null || true

# ── Enable IP forwarding ──────────────────────────────────────────────────────
log "Enabling IP forwarding..."
echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf

# ── Create required directories ───────────────────────────────────────────────
log "Creating directories..."
mkdir -p /opt/pisowifi/db
mkdir -p /opt/pisowifi/portal/assets/banner
mkdir -p /opt/pisowifi/portal/assets/sounds
mkdir -p /var/log

# ── Set permissions ───────────────────────────────────────────────────────────
chmod 755 /opt/pisowifi/scripts/*.sh 2>/dev/null || true
chmod 755 /opt/pisowifi/scripts/*.py 2>/dev/null || true

log "=== Customization complete ==="
