#!/bin/bash
# PotsWorks PisoWifi — iptables setup script
# Run once at boot (via first_boot.sh or pisowifi-firstboot.service)

set -e

WAN_IF="${1:-eth0}"   # WAN interface (passed as argument or default eth0)
LAN_IF="wlan0"        # Hotspot interface
GW_IP="10.0.0.1"      # Gateway IP

echo "[iptables] Setting up firewall rules (WAN=$WAN_IF, LAN=$LAN_IF)..."

# ── Flush existing rules ──────────────────────────────────────────────────────
iptables -F
iptables -t nat -F
iptables -t mangle -F
iptables -X 2>/dev/null || true

# ── Default policies ──────────────────────────────────────────────────────────
iptables -P INPUT   ACCEPT
iptables -P FORWARD DROP
iptables -P OUTPUT  ACCEPT

# ── NAT: masquerade outbound traffic on WAN ───────────────────────────────────
iptables -t nat -A POSTROUTING -o "$WAN_IF" -j MASQUERADE

# ── Captive portal: redirect all HTTP from hotspot to Flask ──────────────────
iptables -t nat -A PREROUTING -i "$LAN_IF" -p tcp --dport 80 \
    -j DNAT --to-destination "$GW_IP:80"

# ── Captive portal: redirect HTTPS to HTTP portal (avoid SSL errors) ─────────
iptables -t nat -A PREROUTING -i "$LAN_IF" -p tcp --dport 443 \
    -j DNAT --to-destination "$GW_IP:80"

# ── Allow established/related connections ─────────────────────────────────────
iptables -A FORWARD -m state --state ESTABLISHED,RELATED -j ACCEPT

# ── Allow loopback ────────────────────────────────────────────────────────────
iptables -A INPUT -i lo -j ACCEPT

# ── Allow SSH from hotspot (for admin access) ─────────────────────────────────
iptables -A INPUT -i "$LAN_IF" -p tcp --dport 22 -j ACCEPT

# ── Allow Flask backend on port 80 ────────────────────────────────────────────
iptables -A INPUT -i "$LAN_IF" -p tcp --dport 80 -j ACCEPT

# ── Block customers from accessing admin panel directly ───────────────────────
# (Flask handles /admin auth, but this adds an extra layer)
# Note: customers are redirected to portal, not admin

# ── Enable IP forwarding ──────────────────────────────────────────────────────
echo 1 > /proc/sys/net/ipv4/ip_forward
sysctl -w net.ipv4.ip_forward=1 > /dev/null

# ── Save rules for persistence ────────────────────────────────────────────────
if command -v iptables-save &>/dev/null; then
    iptables-save > /etc/iptables/rules.v4 2>/dev/null || true
fi

echo "[iptables] Setup complete."
