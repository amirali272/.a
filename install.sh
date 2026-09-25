#!/usr/bin/env bash
set -euo pipefail

RED='\033[0;31m'; WHITE='\033[1;37m'; GRAY='\033[0;90m'; NC='\033[0m'
info() { echo -e "${WHITE}[*]${NC} $*"; }
err()  { echo -e "${RED}[x]${NC} $*" >&2; }

# 🔧 اینجا آدرس باینری خودت رو بذار:
BIN_URL="https://example.com/backpack"

BIN_PATH="/usr/local/bin/backpack"
INSTALL_DIR="/root/BackPack"

if [[ $EUID -ne 0 ]]; then err "Please run as root (sudo)."; exit 1; fi

mkdir -p /etc/backpack "$INSTALL_DIR/backups"

info "Downloading binary from: ${BIN_URL}"
curl -fSL --connect-timeout 15 "$BIN_URL" -o "$BIN_PATH" || {
  err "Download failed."
  exit 1
}

chmod +x "$BIN_PATH"
echo "$INSTALL_DIR" > /etc/backpack/install_path

echo
echo -e "${WHITE}Done!${NC}"

if [ -t 0 ]; then
  echo -e "Starting the menu..."
  echo
  exec "$BIN_PATH"
else
  echo -e "Open the menu with:  ${RED}sudo backpack${NC}"
fi
