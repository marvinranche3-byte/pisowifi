#!/bin/bash
set -e

# Find overlay dir
OVERLAY_DIR="${USERPATCHES_PATH}/overlay"
[ -d "$OVERLAY_DIR" ] || OVERLAY_DIR="/root/overlay"

log() { echo "[customize] $*"; }
log "=== PisoWifi Customization ==="
log "OVERLAY_DIR=$OVERLAY_DIR"

apt-get update -qq
apt-get install -y --no-install-recommends python3 python3-pip python3-flask hostapd dnsmasq iptables iptables-persistent sqlite3 iproute2 wireless-tools wpasupplicant logrotate ntp curl vlan

pip3 install flask --break-system-packages 2>&1 || pip3 install flask 2>&1 || true

cp -r "$OVERLAY_DIR/opt/pisowifi" /opt/
cp "$OVERLAY_DIR/etc/hostapd/hostapd.conf" /etc/hostapd/hostapd.conf
cp "$OVERLAY_DIR/etc/dnsmasq.conf" /etc/dnsmasq.conf
cp "$OVERLAY_DIR/etc/network/interfaces" /etc/network/interfaces
cp "$OVERLAY_DIR/etc/logrotate.d/pisowifi" /etc/logrotate.d/pisowifi
cp "$OVERLAY_DIR/lib/systemd/system/pisowifi-"*.service /lib/systemd/system/

systemctl enable hostapd dnsmasq pisowifi-firstboot pisowifi-backend pisowifi-coin pisowifi-session pisowifi-watchdog

sed -i 's/#DAEMON_CONF=""/DAEMON_CONF="\/etc\/hostapd\/hostapd.conf"/' /etc/default/hostapd 2>/dev/null || true
echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf

mkdir -p /opt/pisowifi/db /opt/pisowifi/portal/assets/banner /opt/pisowifi/portal/assets/sounds
chmod 755 /opt/pisowifi/scripts/*.sh 2>/dev/null || true
chmod 755 /opt/pisowifi/scripts/*.py 2>/dev/null || true

log "=== Done ==="
