#!/usr/bin/env bash
#
# Backpack installer — one command on the VPS (as root):
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/AminMGMT/BackPack/main/install.sh)
#
# It downloads the prebuilt release tar.gz for this architecture into
# /root/BackPack and installs the binary. If run inside a source checkout and
# the download fails, it builds from source as a last resort.
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

fetch() {
  local url="$1" out="$2"
  info "Downloading: ${url}"
  curl -fSL --connect-timeout 15 "$url" -o "$out" 2>/dev/null
}

trusted_dir() {
  local dir="$1" perms
  perms="$(stat -c '%a' "$dir" 2>/dev/null)" || return 1
  perms="${perms: -3}"
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
# The checksums Go publishes for the toolchain this build needs.
GO_SHA_VERSION="1.26.6"
GO_SHA256_amd64="708effb774be8237570d0add163225abbdfaf4fca28b2611df167beba4feef89"
GO_SHA256_arm64="d0507e9e9d7fe012aae570108cbd76c15de879e17130ab8cb90d4d7445cb1f2e"
GO_SHA256_386="f09a71029fc5cd2940fbe36b0eb1fb2d8f3407cd6adb6b7b4de3eaf04007f8c4"
GO_SHA256_s390x="958757933d38172dd544085d253c8738cf09793d24c8bc0422e5e1e1fffa4fde"
GO_SHA256_armv6l="e1379a2fe77bd30fa29833074388247e7c65416e09279f746f20de2d5cf4dfea"

go_arch() {
  case "$1" in
    armv*) echo "armv6l" ;;
    *)     echo "$1" ;;
  esac
}

go_sha256() {
  local var="GO_SHA256_$1"
  echo "${!var-}"
}

download_go() {
  local garch file out want got
  garch="$(go_arch "$ARCH")"
  file="go${GO_VERSION}.linux-${garch}.tar.gz"
  out="$1"
  want="$(go_sha256 "$garch")"

  if [[ "$GO_VERSION" != "$GO_SHA_VERSION" ]]; then
    err "This installer carries Go checksums for ${GO_SHA_VERSION}, but go.mod asks"
    err "for ${GO_VERSION}. The toolchain cannot be verified, so it will not be"
    err "downloaded."
    err "Fix: update GO_SHA_VERSION and the GO_SHA256_* values in install.sh from"
    err "     https://go.dev/dl/?mode=json&include=all"
    err "Or install Go ${GO_VERSION} or newer yourself and run this again."
    return 1
  fi
  if [[ -z "$want" ]]; then
    err "No pinned Go checksum for ${garch} in this installer, so the toolchain"
    err "cannot be verified and will not be downloaded."
    err "Install Go ${GO_VERSION} or newer yourself and run this again, or use"
    err "the offline install — see the README."
    return 1
  fi

  for u in "https://go.dev/dl/${file}" \
           "https://golang.google.cn/dl/${file}" \
           "https://mirrors.aliyun.com/golang/${file}"; do
    info "Trying ${u}"
    curl -fsSL --connect-timeout 15 "$u" -o "$out" || { warn "source failed, trying next..."; continue; }

    if command -v sha256sum >/dev/null 2>&1; then
      got="$(sha256sum "$out" | awk '{print $1}')"
    elif command -v shasum >/dev/null 2>&1; then
      got="$(shasum -a 256 "$out" | awk '{print $1}')"
    else
      err "Neither sha256sum nor shasum is available, so the Go toolchain cannot"
      err "be verified. Refusing to unpack it."
      rm -f "$out"
      return 1
    fi

    if [[ "$got" == "$want" ]]; then
      info "Go toolchain checksum verified: ${got:0:16}..."
      return 0
    fi

    err "CHECKSUM MISMATCH for ${file} from ${u}"
    err "  expected: ${want}"
    err "  actual:   ${got}"
    err "That source served something other than the published toolchain."
    rm -f "$out"
    warn "trying next source..."
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

if [ -t 0 ]; then
  echo -e "Starting the menu... ${GRAY}(next time, just run ${NC}${RED}sudo backpack${GRAY})${NC}"
  echo
  exec "$BIN_PATH"
else
  echo -e "Open the menu with:  ${RED}sudo backpack${NC}"
fi
