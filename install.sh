#!/bin/sh
# install.sh: the curl|sh installer for vcharon, on Linux and macOS.
# Usage: curl -fsSL https://raw.githubusercontent.com/zhoufanscut/VCharon/main/install.sh | sh
#
# Finds this machine's OS and architecture, downloads the matching archive of the latest GitHub
# release, checks it against the release's .sha256 (a mismatch fails), tries the binary, and
# puts it at ~/.local/bin/vcharon; then says whether that folder is on your PATH. Windows has
# install.ps1. The same steps as vcharon --update (DESIGN, "Self-update").
#
# Everything is in main(), called on the last line: a download cut short runs nothing.
set -e

REPO="zhoufanscut/VCharon"
BIN_NAME="vcharon"
PIPX_LINE="  pipx install git+https://github.com/${REPO}"
# the new binary's --version: a one-file binary unpacks itself first (the installer's tests
# shorten it)
VERSION_TIMEOUT="${VCHARON_INSTALL_VERSION_TIMEOUT:-60}"

# A failure's line on stderr, then exit 1.
die() {
  echo "error: $*" >&2
  exit 1
}

# curl, https only, redirects included. A local fake release (the installer's tests) may be
# plain http, on this machine only.
fetch() {
  curl --proto "${PROTO}" --proto-redir '=https' "$@"
}

# Whether a URL override is one the tests use: https anywhere, http only to this machine
# (host exactly 127.0.0.1 or localhost, an optional numeric port). Never one with an @: a user
# part would make the text before it look like the host.
local_or_https() {
  case "$1" in
    *@*) return 1 ;;
    https://*) return 0 ;;
    http://*) ;;
    *) return 1 ;;
  esac
  hostport="${1#http://}"
  hostport="${hostport%%/*}"
  case "${hostport}" in
    127.0.0.1|localhost) return 0 ;;
    127.0.0.1:*|localhost:*)
      port="${hostport#*:}"
      case "${port}" in
        ""|*[!0-9]*) return 1 ;;
        *) return 0 ;;
      esac
      ;;
    *) return 1 ;;
  esac
}

sha256_of() {
  if command -v sha256sum > /dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum > /dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

# "$1" --version's output, or nothing if it fails or takes over VERSION_TIMEOUT seconds.
version_of() {
  if command -v timeout > /dev/null 2>&1; then
    timeout "${VERSION_TIMEOUT}" "$1" --version 2>/dev/null || true
    return 0
  fi
  # no timeout(1), as on macOS: the binary in the background, and a watcher that stops it
  # after VERSION_TIMEOUT: TERM first (a one-file bootloader passes it on to its child), then
  # KILL after a short grace
  "$1" --version > "${WORK_DIR}/version.out" 2>/dev/null &
  pid=$!
  (
    # the binary done in time: the outer shell TERMs this watcher, which ends its sleep too
    sleeper=""
    trap 'if [ -n "${sleeper}" ]; then kill "${sleeper}" 2>/dev/null; fi; exit 0' TERM
    sleep "${VERSION_TIMEOUT}" &
    sleeper=$!
    wait "${sleeper}"
    kill "${pid}" 2>/dev/null || exit 0
    sleep 5
    kill -9 "${pid}" 2>/dev/null
  ) > /dev/null 2>&1 &
  killer=$!
  wait "${pid}" 2>/dev/null || true
  kill "${killer}" 2>/dev/null || true
  wait "${killer}" 2>/dev/null || true
  cat "${WORK_DIR}/version.out"
}

main() {
  BIN_DIR="${HOME}/.local/bin"
  # Where the release is read from. Only the installer's own tests set these, to a local
  # fake release; nothing else should.
  API_URL="${VCHARON_INSTALL_API_URL:-https://api.github.com/repos/${REPO}/releases/latest}"
  DOWNLOAD_ROOT="${VCHARON_INSTALL_DOWNLOAD_URL:-https://github.com/${REPO}/releases/download}"
  PROTO='=https'
  if [ -n "${VCHARON_INSTALL_API_URL:-}${VCHARON_INSTALL_DOWNLOAD_URL:-}" ]; then
    for url in "${API_URL}" "${DOWNLOAD_ROOT}"; do
      local_or_https "${url}" || die "an install URL must be https, or http to this machine: ${url}"
    done
    PROTO='=http,https'
  fi

  # --- this machine ---
  OS="$(uname -s)"
  case "${OS}" in
    Linux*)  PLATFORM="linux" ;;
    Darwin*) PLATFORM="darwin" ;;
    *) die "unsupported OS: ${OS} (on Windows, use install.ps1)" ;;
  esac

  ARCH="$(uname -m)"
  case "${ARCH}" in
    x86_64|amd64)
      if [ "${PLATFORM}" = "darwin" ]; then
        echo "error: no binary is published for an Intel Mac (darwin-x64); install with pipx:" >&2
        echo "${PIPX_LINE}" >&2
        exit 1
      fi
      ARCH_TAG="x64"
      ;;
    arm64|aarch64)
      if [ "${PLATFORM}" = "linux" ]; then
        echo "error: no binary is published for Linux arm64; install with pipx:" >&2
        echo "${PIPX_LINE}" >&2
        exit 1
      fi
      ARCH_TAG="arm64"
      ;;
    *) die "unsupported architecture: ${ARCH}" ;;
  esac

  ASSET="${BIN_NAME}-${PLATFORM}-${ARCH_TAG}"
  TARBALL="${ASSET}.tar.gz"

  command -v curl > /dev/null 2>&1 || die "curl is required"

  # --- the latest release's tag ---
  echo "Fetching the latest release from ${API_URL} ..."
  RELEASE_JSON="$(fetch -fsSL "${API_URL}")"
  # no JSON parser assumed: POSIX sh, grep and sed
  TAG="$(printf '%s\n' "${RELEASE_JSON}" | grep '"tag_name"' | head -n1 \
    | sed 's/.*"tag_name": *"\([^"]*\)".*/\1/')"
  [ -n "${TAG}" ] || die "could not read the latest release's tag"
  VERSION="${TAG#v}"

  DOWNLOAD_URL="${DOWNLOAD_ROOT}/${TAG}/${TARBALL}"
  CHECKSUM_URL="${DOWNLOAD_URL}.sha256"

  # --- download and check ---
  WORK_DIR="$(mktemp -d)"

  echo "Downloading ${TARBALL} (${TAG}) ..."
  fetch -fsSL --output "${WORK_DIR}/${TARBALL}" "${DOWNLOAD_URL}"

  # Only a release without a checksum file (404) goes on unchecked; any other failure to get
  # it is an error, never "not checked".
  if ! CODE="$(fetch -sSL -w '%{http_code}' --output "${WORK_DIR}/${TARBALL}.sha256" \
      "${CHECKSUM_URL}")"; then
    die "couldn't download ${TARBALL}.sha256; nothing was installed"
  fi
  case "${CODE}" in
    200)
      echo "Checking the checksum ..."
      # one line, "<hex>  <file name>": the first word counts, as for vcharon --update
      EXPECTED="$(awk 'NR == 1 {print tolower($1)}' "${WORK_DIR}/${TARBALL}.sha256")"
      case "${EXPECTED}" in
        *[!0-9a-f]*|"") die "${TARBALL}.sha256 holds no sha256" ;;
      esac
      [ "${#EXPECTED}" -eq 64 ] || die "${TARBALL}.sha256 holds no sha256"
      ACTUAL="$(sha256_of "${WORK_DIR}/${TARBALL}")"
      if [ -z "${ACTUAL}" ]; then
        echo "warning: no sha256sum or shasum here; the checksum is not checked" >&2
      elif [ "${ACTUAL}" != "${EXPECTED}" ]; then
        die "checksum mismatch for ${TARBALL}: expected ${EXPECTED}, got ${ACTUAL};" \
          "nothing was installed"
      fi
      ;;
    404)
      echo "warning: this release has no checksum file; the checksum is not checked" >&2
      ;;
    *)
      die "couldn't download ${TARBALL}.sha256 (HTTP ${CODE}); nothing was installed"
      ;;
  esac

  # Only the binary, never the whole archive, and only its bytes: a link member named
  # vcharon writes nothing here, and the --version check below fails.
  echo "Extracting ..."
  tar -xzOf "${WORK_DIR}/${TARBALL}" "${BIN_NAME}" > "${WORK_DIR}/${BIN_NAME}" \
    || die "${TARBALL} holds no ${BIN_NAME}"
  chmod +x "${WORK_DIR}/${BIN_NAME}"

  # the binary must run here and be the release's version; a Linux older than the build
  # machine's glibc fails here, before anything is replaced
  GOT="$(version_of "${WORK_DIR}/${BIN_NAME}")"
  if [ "${GOT}" != "${VERSION}" ]; then
    echo "error: the downloaded binary didn't run here (its --version printed '${GOT}'," \
      "expected '${VERSION}'); nothing was installed. Install with pipx instead:" >&2
    echo "${PIPX_LINE}" >&2
    exit 1
  fi

  # --- install ---
  # copied next to the target, then renamed over it: one filesystem, so the swap is atomic
  # and a running vcharon keeps its own file (its watchers end with EXIT updated)
  mkdir -p "${BIN_DIR}"
  DEST="${BIN_DIR}/${BIN_NAME}"
  STAGED="${BIN_DIR}/.${BIN_NAME}.new.$$"
  cp "${WORK_DIR}/${BIN_NAME}" "${STAGED}"
  chmod 755 "${STAGED}"
  mv -f "${STAGED}" "${DEST}"

  echo ""
  echo "Installed: ${DEST} (${VERSION})"

  # --- PATH hint ---
  case ":${PATH}:" in
    *":${BIN_DIR}:"*)
      ;;
    *)
      echo ""
      echo "  ${BIN_DIR} is not on your PATH."
      echo "  Add this line to your shell profile (~/.bashrc, ~/.zshrc, ...):"
      echo ""
      echo "    export PATH=\"\${HOME}/.local/bin:\${PATH}\""
      echo ""
      ;;
  esac

  echo "Run \`vcharon --version\` to check it, then \`vcharon guide\`."
}

WORK_DIR=""
STAGED=""
cleanup() {
  if [ -n "${WORK_DIR}" ]; then rm -rf "${WORK_DIR}"; fi
  if [ -n "${STAGED}" ]; then rm -f "${STAGED}"; fi
}
trap cleanup EXIT
# a signal goes through exit, so the EXIT trap cleans up in every shell (dash runs no EXIT
# trap for a signal it doesn't catch)
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

main "$@"
