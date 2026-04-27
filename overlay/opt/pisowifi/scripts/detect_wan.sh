#!/bin/bash
# PotsWorks PisoWifi — Automatic Network Interface Detection
#
# RULE: eth0 (built-in LAN) is ALWAYS the WAN port.
#
# Three auto-detected setups:
#
# SETUP A: VLAN Mode (5v5 Modem)
#   Condition: VLAN ID is configured in DB (e.g., VLAN 22)
#   WAN     = eth0.22 (built-in LAN + VLAN 22 tag)
#   AP/LAN  = usb0 (USB-to-LAN adapter, if present) — wired clients
#   Hotspot = wlan0 (built-in WiFi) — wireless clients
#   Example: 5v5 modem LAN1 → OPi eth0, modem LAN4 → ISP
#
# SETUP B: USB-to-LAN as AP Mode
#   Condition: USB-to-LAN adapter is plugged in, no VLAN configured
#   WAN     = eth0 (built-in LAN, direct to modem)
#   AP/LAN  = usb0 (USB-to-LAN adapter) — wired clients as AP
#   Hotspot = wlan0 (built-in WiFi) — wireless clients
#
# SETUP C: Direct Mode (fallback)
#   Condition: No USB adapter, no VLAN
#   WAN     = eth0 (built-in LAN, direct to modem)
#   Hotspot = wlan0 (built-in WiFi) — wireless clients only

set -e

DB_PATH="/opt/pisowifi/db/pisowifi.db"
IPTABLES_SCRIPT="/opt/pisowifi/scripts/setup_iptables.sh"
LOG_FILE="/var/log/pisowifi-network.log"

log() {
    local msg="[detect_wan] $(date '+%Y-%m-%d %H:%M:%S') $*"
    echo "$msg"
    echo "$msg" >> "$LOG_FILE" 2>/dev/null || true
}

# ── Read config from DB ───────────────────────────────────────────────────────
get_config() {
    local key="$1" default="$2"
    if [ -f "$DB_PATH" ]; then
        local val
        val=$(sqlite3 "$DB_PATH" "SELECT value FROM config WHERE key='$key';" 2>/dev/null)
        echo "${val:-$default}"
    else
        echo "$default"
    fi
}

save_config() {
    local key="$1" val="$2"
    [ -f "$DB_PATH" ] && sqlite3 "$DB_PATH" \
        "INSERT OR REPLACE INTO config (key,value) VALUES ('$key','$val');" 2>/dev/null || true
}

# ── Check if a USB ethernet adapter is present ───────────────────────────────
find_usb_ethernet() {
    # Check common USB ethernet interface names
    for iface in usb0 usb1 eth1 eth2; do
        if [ -d "/sys/class/net/$iface" ]; then
            # Verify it's actually a USB device
            if readlink "/sys/class/net/$iface" 2>/dev/null | grep -q usb; then
                echo "$iface"; return 0
            fi
        fi
    done
    # Check enx* (USB ethernet with MAC-based names)
    for path in /sys/class/net/enx*; do
        local iface
        iface=$(basename "$path")
        if [ -d "$path" ] && readlink "$path" 2>/dev/null | grep -q usb; then
            echo "$iface"; return 0
        fi
    done
    return 1
}

# ── Configure USB-to-LAN as AP/LAN bridge ────────────────────────────────────
setup_usb_ap() {
    local usb_if="$1"
    log "Configuring $usb_if as AP/LAN interface..."
    ip link set "$usb_if" up

    # Assign a static IP on the LAN side for wired clients
    # (dnsmasq will serve DHCP on this interface too if configured)
    ip addr add 10.0.1.1/24 dev "$usb_if" 2>/dev/null || true

    log "$usb_if ready as AP/LAN (10.0.1.1/24)"
}

# ── Setup VLAN on eth0 ────────────────────────────────────────────────────────
setup_vlan() {
    local vlan_id="$1"
    local vlan_if="eth0.${vlan_id}"

    log "Loading 8021q VLAN module..."
    modprobe 8021q 2>/dev/null || true

    # Remove existing VLAN interface if present
    ip link del "$vlan_if" 2>/dev/null || true

    log "Creating VLAN interface: $vlan_if (VLAN $vlan_id on eth0)"
    ip link add link eth0 name "$vlan_if" type vlan id "$vlan_id"
    ip link set eth0 up
    ip link set "$vlan_if" up

    # Get IP via DHCP on VLAN interface
    log "Requesting DHCP on $vlan_if..."
    dhclient "$vlan_if" 2>/dev/null &
    sleep 3  # Give DHCP time to respond

    log "VLAN interface ready: $vlan_if"
    echo "$vlan_if"
}

# ── Setup direct DHCP on eth0 ─────────────────────────────────────────────────
setup_direct() {
    log "Requesting DHCP on eth0..."
    ip link set eth0 up
    dhclient eth0 2>/dev/null &
    sleep 3
    log "eth0 DHCP configured"
}

# ════════════════════════════════════════════════════════════════════════════
# MAIN AUTO-DETECTION
# ════════════════════════════════════════════════════════════════════════════

log "=== PotsWorks PisoWifi — Network Auto-Detection ==="
log "RULE: eth0 (built-in LAN) is always WAN"

VLAN_ID=$(get_config "vlan_id" "0")
USB_IF=$(find_usb_ethernet 2>/dev/null || echo "")
ACTUAL_WAN=""
SETUP_TYPE=""

# ── Priority 1: VLAN configured → Setup A ────────────────────────────────────
if [ "$VLAN_ID" != "0" ] && [ -n "$VLAN_ID" ]; then
    log "VLAN $VLAN_ID detected → SETUP A (VLAN Mode)"
    log "  Modem LAN1 → eth0 (built-in LAN) → eth0.${VLAN_ID} = WAN"
    log "  Modem LAN4 = ISP (WAN ng modem)"
    log "  wlan0 (built-in WiFi) = Hotspot para sa customers"
    log "  Walang USB-to-LAN sa setup na ito"

    SETUP_TYPE="vlan"
    ACTUAL_WAN=$(setup_vlan "$VLAN_ID")
    # NOTE: No USB-to-LAN in VLAN mode — wlan0 is the only AP

# ── Priority 2: USB-to-LAN present, no VLAN → Setup B ────────────────────────
elif [ -n "$USB_IF" ]; then
    log "USB-to-LAN adapter ($USB_IF) detected, no VLAN → SETUP B (USB-to-LAN AP Mode)"
    log "  eth0 (built-in LAN) = WAN (direct to modem)"
    log "  $USB_IF (USB-to-LAN adapter) = AP/LAN (wired clients, 10.0.1.1/24)"
    log "  wlan0 (built-in WiFi) = Hotspot (wireless clients)"

    SETUP_TYPE="usb_ap"
    setup_direct
    ACTUAL_WAN="eth0"
    setup_usb_ap "$USB_IF"

# ── Priority 3: No USB, no VLAN → Setup C (direct) ───────────────────────────
else
    log "No USB adapter, no VLAN → SETUP C (Direct Mode)"
    log "  eth0 (built-in LAN) = WAN (direct to modem)"
    log "  wlan0 (built-in WiFi) = Hotspot"

    SETUP_TYPE="direct"
    setup_direct
    ACTUAL_WAN="eth0"
fi

# ── Save detected setup to DB ─────────────────────────────────────────────────
save_config "detected_setup"  "$SETUP_TYPE"
save_config "detected_wan_if" "$ACTUAL_WAN"
[ -n "$USB_IF" ] && save_config "detected_usb_if" "$USB_IF"

# ── Apply iptables/NAT rules ──────────────────────────────────────────────────
log "Applying iptables rules (WAN=$ACTUAL_WAN)..."
bash "$IPTABLES_SCRIPT" "$ACTUAL_WAN"

log "=== Network setup complete: $SETUP_TYPE (WAN=$ACTUAL_WAN) ==="

set -e

DB_PATH="/opt/pisowifi/db/pisowifi.db"
IPTABLES_SCRIPT="/opt/pisowifi/scripts/setup_iptables.sh"
LOG_FILE="/var/log/pisowifi-network.log"

log() {
    local msg="[detect_wan] $(date '+%Y-%m-%d %H:%M:%S') $*"
    echo "$msg"
    echo "$msg" >> "$LOG_FILE" 2>/dev/null || true
}

# ── Read config from DB ───────────────────────────────────────────────────────
get_config() {
    local key="$1"
    local default="$2"
    if [ -f "$DB_PATH" ]; then
        local val
        val=$(sqlite3 "$DB_PATH" "SELECT value FROM config WHERE key='$key';" 2>/dev/null)
        echo "${val:-$default}"
    else
        echo "$default"
    fi
}

# ── Check if interface has physical link ──────────────────────────────────────
has_link() {
    local iface="$1"
    [ -f "/sys/class/net/$iface/carrier" ] && [ "$(cat /sys/class/net/$iface/carrier 2>/dev/null)" = "1" ]
}

# ── Check if interface is a USB device ───────────────────────────────────────
is_usb_interface() {
    local iface="$1"
    readlink "/sys/class/net/$iface" 2>/dev/null | grep -q usb
}

# ── Detect USB ethernet adapters ─────────────────────────────────────────────
find_usb_ethernet() {
    for iface in usb0 usb1 eth1 eth2; do
        if [ -d "/sys/class/net/$iface" ]; then
            if is_usb_interface "$iface"; then
                echo "$iface"
                return 0
            fi
        fi
    done
    # Also check enx* (USB ethernet with MAC-based names)
    for iface in /sys/class/net/enx*; do
        local name
        name=$(basename "$iface")
        if [ -d "$iface" ] && is_usb_interface "$name"; then
            echo "$name"
            return 0
        fi
    done
    return 1
}

# ── Load 8021q VLAN module ────────────────────────────────────────────────────
load_vlan_module() {
    modprobe 8021q 2>/dev/null || true
    # Verify it loaded
    lsmod 2>/dev/null | grep -q 8021q && log "8021q VLAN module loaded" || log "Warning: 8021q module not available"
}

# ── Setup VLAN interface ──────────────────────────────────────────────────────
setup_vlan() {
    local base_if="$1"
    local vlan_id="$2"
    local vlan_if="${base_if}.${vlan_id}"

    load_vlan_module

    # Remove existing VLAN interface if present
    ip link del "$vlan_if" 2>/dev/null || true

    # Create VLAN interface
    ip link add link "$base_if" name "$vlan_if" type vlan id "$vlan_id"
    ip link set "$base_if" up
    ip link set "$vlan_if" up

    # Get IP via DHCP
    dhclient -v "$vlan_if" 2>&1 | head -20 || true

    log "VLAN interface ready: $vlan_if (VLAN $vlan_id on $base_if)"
    echo "$vlan_if"
}

# ── Setup direct DHCP ─────────────────────────────────────────────────────────
setup_dhcp() {
    local iface="$1"
    ip link set "$iface" up
    dhclient -v "$iface" 2>&1 | head -20 || true
    log "DHCP configured on $iface"
}

# ── Save detected setup to DB ─────────────────────────────────────────────────
save_network_config() {
    local setup_type="$1"
    local wan_if="$2"
    if [ -f "$DB_PATH" ]; then
        sqlite3 "$DB_PATH" "INSERT OR REPLACE INTO config (key,value) VALUES ('detected_setup','$setup_type');" 2>/dev/null || true
        sqlite3 "$DB_PATH" "INSERT OR REPLACE INTO config (key,value) VALUES ('detected_wan_if','$wan_if');" 2>/dev/null || true
    fi
}

# ════════════════════════════════════════════════════════════════════════════
# MAIN DETECTION LOGIC
# ════════════════════════════════════════════════════════════════════════════

log "=== PotsWorks PisoWifi — Network Auto-Detection ==="

# Read VLAN config from DB
VLAN_ID=$(get_config "vlan_id" "0")
ACTUAL_WAN=""
SETUP_TYPE=""

# ── DETECTION STEP 1: Check for USB-to-LAN adapter ───────────────────────────
USB_IF=$(find_usb_ethernet 2>/dev/null || echo "")

if [ -n "$USB_IF" ]; then
    # ── SETUP B: USB-to-LAN Mode ──────────────────────────────────────────────
    log "USB ethernet adapter detected: $USB_IF → SETUP B (USB-to-LAN Mode)"
    log "  WAN = $USB_IF (USB-to-LAN adapter)"
    log "  LAN = eth0 (built-in ethernet, wired clients)"
    log "  Hotspot = wlan0 (built-in WiFi)"

    SETUP_TYPE="usb_to_lan"
    setup_dhcp "$USB_IF"
    ACTUAL_WAN="$USB_IF"

    # Update DB: switch to USB-to-LAN mode
    save_network_config "$SETUP_TYPE" "$ACTUAL_WAN"

elif [ "$VLAN_ID" != "0" ] && [ -n "$VLAN_ID" ]; then
    # ── SETUP A: VLAN Mode ────────────────────────────────────────────────────
    log "VLAN $VLAN_ID configured, no USB adapter → SETUP A (VLAN Mode)"
    log "  WAN = eth0.${VLAN_ID} (built-in ethernet + VLAN $VLAN_ID)"
    log "  Hotspot = wlan0 (built-in WiFi)"

    SETUP_TYPE="vlan"
    ACTUAL_WAN=$(setup_vlan "eth0" "$VLAN_ID")
    save_network_config "$SETUP_TYPE" "$ACTUAL_WAN"

else
    # ── SETUP C: Direct Ethernet (fallback) ───────────────────────────────────
    log "No USB adapter, no VLAN → SETUP C (Direct Ethernet)"
    log "  WAN = eth0 (built-in ethernet, direct modem connection)"
    log "  Hotspot = wlan0 (built-in WiFi)"

    SETUP_TYPE="direct"
    setup_dhcp "eth0"
    ACTUAL_WAN="eth0"
    save_network_config "$SETUP_TYPE" "$ACTUAL_WAN"
fi

# ── Apply iptables rules ──────────────────────────────────────────────────────
log "Applying iptables rules (WAN=$ACTUAL_WAN, Setup=$SETUP_TYPE)..."
bash "$IPTABLES_SCRIPT" "$ACTUAL_WAN"

log "=== Network setup complete: $SETUP_TYPE (WAN=$ACTUAL_WAN) ==="
