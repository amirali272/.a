#!/usr/bin/env bash
#
# Backpack installer — one command on the VPS (as root):
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/AminMGMT/BackPack/main/install.sh)
#
# It downloads the prebuilt release tar.gz for this architecture into
# /root/BackPack and installs the binary.
# If run inside a source checkout and the download fails, it builds from
# source as a last resort.
#
# When it finishes it opens the menu automatically (on an interactive terminal).
# Later, reopen it any time with:  sudo backpack
#
set -euo pipefail

RED='\033[0;31m'; WHITE='\033[1;37m'; GRAY='\033[0;90m'; NC='\033[0m'
info() { echo -e "${WHITE}[*]${NC} $*"; }
warn() { echo -e "${GRAY}[!]${NC} $*"; }
err()  { echo -e "${RED}[x]${NC} $*" >&2; }

REPO="AminMGMT/BackPack"
BIN_PATH="/usr/local/bin/backpack"
INSTALL_DIR="/root/BackPack"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-/tmp}")" 2>/dev/null && pwd || echo /tmp)"

# Which Go the source build needs, and the oldest toolchain already on the
# machine that is usable for it. Read from go.mod whenever it is beside this
# script, because go.mod is what actually decides.
GO_VERSION="1.26.6"
GO_MIN_MINOR=26
if [[ -f "$SCRIPT_DIR/go.mod" ]]; then
  gomod_go="$(grep -m1 -E '^go[[:space:]]+[0-9]+\.[0-9]+' "$SCRIPT_DIR/go.mod" | awk '{print $2}' || true)"
  if [[ "$gomod_go" =~ ^[0-9]+\.([0-9]+)(\.[0-9]+)?$ ]]; then
    if [[ -n "${BASH_REMATCH[2]}" ]]; then GO_VERSION="$gomod_go"; else GO_VERSION="${gomod_go}.0"; fi
    GO_MIN_MINOR="${BASH_REMATCH[1]}"
  fi
  unset gomod_go
fi

if [[ $EUID -ne 0 ]]; then err "Please run as root (sudo)."; exit 1; fi

if [[ $# -gt 0 ]]; then
  err "Unknown argument: $1"
  err "This script takes no arguments. Run it to install Backpack."
  err "To have a panel manage this server, add it from the panel; nothing is needed here."
  exit 2
fi

# Which release asset this machine can run.
arm_variant() {
  case "$(uname -m)" in
    armv7*) echo 7; return ;;
    armv6*) echo 6; return ;;
    armv5*|armv4*) echo 5; return ;;
  esac
  case "$(grep -m1 -i '^CPU architecture' /proc/cpuinfo 2>/dev/null)" in
    *7*) echo 7 ;;
    *6*) echo 6 ;;
    *5*) echo 5 ;;
    *)   echo 6 ;;
  esac
}

case "$(uname -m)" in
  x86_64|amd64)   ARCH="amd64" ;;
  aarch64|arm64)  ARCH="arm64" ;;
  i386|i486|i586|i686) ARCH="386" ;;
  s390x)          ARCH="s390x" ;;
  armv*|arm)      ARCH="armv$(arm_variant)" ;;
  *) err "Unsupported architecture: $(uname -m)"; exit 1 ;;
esac

ASSET="backpack_linux_${ARCH}.tar.gz"
mkdir -p /etc/backpack "$INSTALL_DIR/backups"

# fetch <url> <out> — straight to GitHub, so TLS terminates there.
fetch() {
  local url="$1" out="$2"
  info "Downloading: ${url}"
  curl -fSL --connect-timeout 15 "$url" -o "$out" 2>/dev/null
}

# trusted_dir <dir> — true when an arbitrary local account cannot put a file in it.
trusted_dir() {
  local dir="$1" perms
  perms="$(stat -c '%a' "$dir" 2>/dev/null)" || return 1
  perms="${perms: -3}"   # drop setuid/sticky if stat printed four digits
  (( (${perms:2:1} & 2) == 0 ))
}

install_release() {
  # 1) A local release asset next to the script (e.g. ./release/ or ./dist/).
  for cand in "$SCRIPT_DIR/release/$ASSET" "$SCRIPT_DIR/dist/$ASSET" "$SCRIPT_DIR/$ASSET"; do
    if [[ -f "$cand" ]]; then
      local canddir; canddir="$(dirname "$cand")"
      if ! trusted_dir "$canddir"; then
        warn "Ignoring ${cand}: ${canddir} is world-writable."
        warn "Work from a directory only you can write — /root is what docs/install.md uses."
        continue
      fi
      info "Using local release asset: ${cand}"
      cp "$cand" "$INSTALL_DIR/$ASSET"
      return 0
    fi
  done

  # 2) The latest GitHub release.
  fetch "https://github.com/${REPO}/releases/latest/download/${ASSET}" "$INSTALL_DIR/$ASSET" || return 1
  return 0
}

install_binary_from_tar() {
  tar -xzf "$INSTALL_DIR/$ASSET" -C "$INSTALL_DIR" backpack
  install -m 0755 "$INSTALL_DIR/backpack" "$BIN_PATH"
  rm -f "$INSTALL_DIR/backpack"
  echo "$INSTALL_DIR" > /etc/backpack/install_path
}

# ---------------------------------------------------------------------------
# Build-from-source fallback (only used when the release download fails and
# this script sits inside a source checkout).
# ---------------------------------------------------------------------------

# go_arch maps this script's asset architecture onto the one Go names its
# toolchain with.
go_arch() {
  case "$1" in
    armv*) echo "armv6l" ;;   # one 32-bit ARM toolchain, usable on v6 and v7
    *)     echo "$1" ;;
  esac
}

download_go() {
  local garch file out
  garch="$(go_arch "$ARCH")"
  file="go${GO_VERSION}.linux-${garch}.tar.gz"
  out="$1"

  for u in "https://go.dev/dl/${file}" \
           "https://golang.google.cn/dl/${file}" \
           "https://mirrors.aliyun.com/golang/${file}"; do
    info "Trying ${u}"
    curl -fsSL --connect-timeout 15 "$u" -o "$out" || { warn "source failed, trying next..."; continue; }
    info "Downloaded Go toolchain from ${u}"
    return 0
  done
  return 1
}

go_new_enough() {
  local v; v="$("$1" version 2>/dev/null | grep -oE 'go1\.[0-9]+' | head -1)"; v="${v#go1.}"
  [[ -n "$v" ]] && (( v >= GO_MIN_MINOR ))
}

ensure_go() {
  command -v go >/dev/null 2>&1 && go_new_enough "$(command -v go)" && { info "Go: $(go version)"; return; }
  [[ -x /usr/local/go/bin/go ]] && go_new_enough /usr/local/go/bin/go && { export PATH="/usr/local/go/bin:$PATH"; info "Go: $(go version)"; return; }
  warn "Installing Go ${GO_VERSION}..."; download_go /tmp/go-bp.tgz || { err "Could not obtain Go."; exit 1; }
  rm -rf /usr/local/go && tar -C /usr/local -xzf /tmp/go-bp.tgz; export PATH="/usr/local/go/bin:$PATH"; info "$(go version)"
}

build_from_source() {
  cd "$SCRIPT_DIR"
  ensure_go; export PATH="/usr/local/go/bin:$PATH"
  # Direct module fetching first, Iran-friendly mirrors as fallback.
  export GOPROXY="https://proxy.golang.org,https://mirror-go.runflare.com,https://goproxy.cn,direct"
  export GOSUMDB=off GOTOOLCHAIN=local
  info "Building from source (proxy order: direct first, then mirrors)."
  CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o "$BIN_PATH" .
  echo "$INSTALL_DIR" > /etc/backpack/install_path
}

if install_release; then
  install_binary_from_tar
  info "Installed release binary -> ${BIN_PATH}"
elif [[ -f "$SCRIPT_DIR/go.mod" && -f "$SCRIPT_DIR/main.go" ]] && trusted_dir "$SCRIPT_DIR"; then
  warn "Release download failed — building from source instead."
  build_from_source
  info "Built and installed -> ${BIN_PATH}"
else
  err "Could not download the release, and no usable source checkout was found here."
  err "(A checkout in a world-writable directory is not built from: it would"
  err " compile whatever is there into a binary that then runs as root.)"
  err "This server may not be able to reach GitHub. Install offline instead:"
  err "download the archive on a machine that can, copy it over, and follow the"
  err "offline steps in the README. Or clone the repo and run install.sh inside it."
  exit 1
fi

chmod +x "$BIN_PATH"
echo
echo -e "${WHITE}Done!${NC}"

# Open the menu straight away — people miss the "now run sudo backpack" step.
if [ -t 0 ]; then
  echo -e "Starting the menu... ${GRAY}(next time, just run ${NC}${RED}sudo backpack${GRAY})${NC}"
  echo
  exec "$BIN_PATH"
else
  echo -e "Open the menu with:  ${RED}sudo backpack${NC}"
fi
