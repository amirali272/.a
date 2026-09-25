#!/usr/bin/env bash
#
# Backpack installer — installs the binary sitting next to this script.
# No GitHub. No release download. No AminMGMT.
#
set -euo pipefail

RED='\033[0;31m'; WHITE='\033[1;37m'; GRAY='\033[0;90m'; NC='\033[0m'
info() { echo -e "${WHITE}[*]${NC} $*"; }
warn() { echo -e "${GRAY}[!]${NC} $*"; }
err()  { echo -e "${RED}[x]${NC} $*" >&2; }

BIN_PATH="/usr/local/bin/backpack"
INSTALL_DIR="/root/BackPack"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-/tmp}")" 2>/dev/null && pwd || echo /tmp)"

if [[ $EUID -ne 0 ]]; then err "Please run as root (sudo)."; exit 1; fi

mkdir -p /etc/backpack "$INSTALL_DIR/backups"

# Find the binary next to this script.
SRC=""
for cand in "$SCRIPT_DIR/backpack" \
            "$SCRIPT_DIR/release/backpack" \
            "$SCRIPT_DIR/dist/backpack" \
            "$SCRIPT_DIR/backpack_linux_amd64/backpack"; do
  if [[ -f "$cand" ]]; then SRC="$cand"; break; fi
done

if [[ -z "$SRC" ]]; then
  err "No 'backpack' binary found next to this script."
  err "Put the compiled binary in the same directory as install.sh and try again."
  err "Looked in: $SCRIPT_DIR, $SCRIPT_DIR/release, $SCRIPT_DIR/dist"
  exit 1
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
