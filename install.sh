#!/usr/bin/env bash
#
# Backpack installer — one command on the VPS (as root):
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/amirali272/.a/main/install.sh)
#
set -euo pipefail

RED='\033[0;31m'; WHITE='\033[1;37m'; GRAY='\033[0;90m'; NC='\033[0m'
info() { echo -e "${WHITE}[*]${NC} $*"; }
warn() { echo -e "${GRAY}[!]${NC} $*"; }
err()  { echo -e "${RED}[x]${NC} $*" >&2; }

# 🔧 آدرس باینری رو اینجا بذار (باینری backpack که خودت build کردی)
BIN_URL="https://github.com/amirali272/.a/releases/latest/download/backpack"

BIN_PATH="/usr/local/bin/backpack"
INSTALL_DIR="/root/BackPack"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-/tmp}")" 2>/dev/null && pwd || echo /tmp)"

if [[ $EUID -ne 0 ]]; then err "Please run as root (sudo)."; exit 1; fi

mkdir -p /etc/backpack "$INSTALL_DIR/backups"

SRC=""
# ۱. اول دنبال باینری کنار اسکریپت بگرد (اگه کاربر دستی گذاشته باشه)
for cand in "$SCRIPT_DIR/backpack" \
            "$SCRIPT_DIR/release/backpack" \
            "$SCRIPT_DIR/dist/backpack"; do
  if [[ -f "$cand" ]]; then SRC="$cand"; break; fi
done

# ۲. اگه پیدا نشد، از URL دانلود کن
if [[ -z "$SRC" ]]; then
  info "Downloading binary from: ${BIN_URL}"
  curl -fSL --connect-timeout 15 "$BIN_URL" -o "$INSTALL_DIR/backpack" || {
    err "Failed to download binary from ${BIN_URL}"
    exit 1
  }
  SRC="$INSTALL_DIR/backpack"
fi

info "Installing binary from: ${SRC}"
install -m 0755 "$SRC" "$BIN_PATH"
echo "$INSTALL_DIR" > /etc/backpack/install_path

chmod +x "$BIN_PATH"
echo
echo -e "${WHITE}Done!${NC}"

if [ -t 0 ]; then
  echo -e "Starting the menu..."
  echo
  exec "$BIN_PATH"
else
  echo -e "Open the menu with:  ${RED}sudo backpack${NC}"
fi
