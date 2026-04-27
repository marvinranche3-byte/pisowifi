#!/bin/bash
# PotsWorks PisoWifi — Armbian Image Build Script
# Builds a custom Armbian image for Orange Pi 1, PC, and Zero 3
#
# Requirements:
#   - Ubuntu 22.04 or Debian Bookworm host (x86_64)
#   - Docker (recommended) or native Armbian build environment
#   - At least 30GB free disk space
#
# Usage:
#   ./build.sh [orangepi1|orangepipc|orangepizero3]
#   ./build.sh all   (build all three boards)

set -e

BOARD="${1:-orangepizero3}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OVERLAY_DIR="$SCRIPT_DIR/overlay"
BUILD_LOG="$SCRIPT_DIR/build_$(date +%Y%m%d_%H%M%S).log"

log() { echo "[build] $(date '+%H:%M:%S') $*" | tee -a "$BUILD_LOG"; }
err() { echo "[build] ERROR: $*" | tee -a "$BUILD_LOG" >&2; exit 1; }

# ── Validate board ────────────────────────────────────────────────────────────
case "$BOARD" in
    orangepi1|orangepipc|orangepizero3|all) ;;
    *) err "Unknown board: $BOARD. Use: orangepi1, orangepipc, orangepizero3, all" ;;
esac

log "=== PotsWorks PisoWifi Image Builder ==="
log "Board: $BOARD"
log "Log: $BUILD_LOG"

# ── Clone Armbian build framework if not present ──────────────────────────────
if [ ! -d "$SCRIPT_DIR/armbian-build" ]; then
    log "Cloning Armbian build framework..."
    git clone --depth=1 https://github.com/armbian/build.git "$SCRIPT_DIR/armbian-build"
fi

# ── Copy userpatches ──────────────────────────────────────────────────────────
log "Copying userpatches..."
mkdir -p "$SCRIPT_DIR/armbian-build/userpatches"
cp "$SCRIPT_DIR/userpatches/customize-image.sh" \
   "$SCRIPT_DIR/armbian-build/userpatches/customize-image.sh"
chmod +x "$SCRIPT_DIR/armbian-build/userpatches/customize-image.sh"

# ── Build function ────────────────────────────────────────────────────────────
build_board() {
    local board="$1"
    log "Building image for $board..."

    cd "$SCRIPT_DIR/armbian-build"
    ./compile.sh \
        BOARD="$board" \
        BRANCH=current \
        RELEASE=bookworm \
        BUILD_MINIMAL=yes \
        BUILD_DESKTOP=no \
        KERNEL_CONFIGURE=no \
        COMPRESS_OUTPUTIMAGE=sha,img \
        USERPATCHES_PATH="$SCRIPT_DIR/armbian-build/userpatches" \
        2>&1 | tee -a "$BUILD_LOG"

    log "Build complete for $board"
    log "Output: $SCRIPT_DIR/armbian-build/output/images/"
}

# ── Run build ─────────────────────────────────────────────────────────────────
if [ "$BOARD" = "all" ]; then
    for b in orangepi1 orangepipc orangepizero3; do
        build_board "$b"
    done
else
    build_board "$BOARD"
fi

log "=== All builds complete ==="
log "Images are in: $SCRIPT_DIR/armbian-build/output/images/"
