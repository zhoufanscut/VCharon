#!/bin/sh
# Runs the installer of this OS against a fake release of the assets just packed (DESIGN,
# "Releases"): install.sh on Linux and macOS, install.ps1 on Windows (Git Bash). The fake
# release is served on 127.0.0.1 through the installers' test-only URL overrides. First with a
# wrong .sha256, which must fail and install nothing; then with the right one, which must
# install the archive's binary, and that binary must print VERSION.
#
#   sh tests/install_check.sh ASSETS_DIR ASSET VERSION WORK_DIR
#
# ASSETS_DIR is tests/pack.py's OUT_DIR; ASSET its platform name (vcharon-linux-x64, …);
# WORK_DIR a scratch folder, made if missing: the fake release, and the installer's HOME (or,
# on Windows, LOCALAPPDATA) go under it. On Windows install.ps1 also adds its folder to the
# user's PATH in the registry, so there it runs only where CI is set.
set -eu

if [ $# -lt 4 ]; then
  echo "usage: sh tests/install_check.sh ASSETS_DIR ASSET VERSION WORK_DIR" >&2
  exit 3
fi
ASSETS="$(cd "$1" && pwd)"
ASSET="$2"
VERSION="$3"
mkdir -p "$4"
WORK="$(cd "$4" && pwd)"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TAG="v${VERSION}"
case "${ASSET}" in
  *-win-x64) ARCHIVE="${ASSET}.zip"; WINDOWS=1 ;;
  *) ARCHIVE="${ASSET}.tar.gz"; WINDOWS="" ;;
esac
if [ -n "${WINDOWS}" ] && [ -z "${CI:-}" ]; then
  echo "install_check: install.ps1 adds a folder to your user PATH; set CI=1 on a runner" >&2
  exit 3
fi
PYTHON="${PYTHON:-}"
if [ -z "${PYTHON}" ]; then
  if command -v python3 > /dev/null 2>&1 && python3 -c "" > /dev/null 2>&1; then
    PYTHON=python3
  else
    PYTHON=python
  fi
fi

fail() {
  echo "install_check: FAIL: $*" >&2
  exit 1
}

# the fake release: releases/latest as latest.json (so it is served as JSON), and the assets
# under download/<tag>/, as GitHub lays them out
RELEASE="${WORK}/release"
rm -rf "${RELEASE}"
mkdir -p "${RELEASE}/download/${TAG}"
printf '{"tag_name": "%s", "name": "%s"}\n' "${TAG}" "${TAG}" > "${RELEASE}/latest.json"
cp "${ASSETS}/${ARCHIVE}" "${RELEASE}/download/${TAG}/"

SERVER=""
cleanup() {
  if [ -n "${SERVER}" ]; then kill "${SERVER}" 2>/dev/null || true; fi
}
trap cleanup EXIT
"${PYTHON}" -u -m http.server 0 --bind 127.0.0.1 --directory "${RELEASE}" \
  > "${WORK}/server.log" 2>&1 &
SERVER=$!
tries=0
until PORT="$(sed -n 's/.* port \([0-9][0-9]*\) .*/\1/p' "${WORK}/server.log")" \
    && [ -n "${PORT}" ]; do
  tries=$((tries + 1))
  if [ "${tries}" -gt 60 ] || ! kill -0 "${SERVER}" 2>/dev/null; then
    cat "${WORK}/server.log"
    fail "the fake release's server didn't start"
  fi
  sleep 0.5
done
ROOT="http://127.0.0.1:${PORT}"
export VCHARON_INSTALL_API_URL="${ROOT}/latest.json"
export VCHARON_INSTALL_DOWNLOAD_URL="${ROOT}/download"

if [ -n "${WINDOWS}" ]; then
  INSTALLED="${WORK}/localappdata/Programs/vcharon/vcharon.exe"
else
  INSTALLED="${WORK}/home/.local/bin/vcharon"
fi

# the installer of this OS, in WORK; its output in WORK/install.out, its exit code returned
run_installer() {
  rm -rf "${WORK}/home" "${WORK}/localappdata"
  mkdir -p "${WORK}/home" "${WORK}/localappdata"
  code=0
  if [ -n "${WINDOWS}" ]; then
    LOCALAPPDATA="$(cygpath -w "${WORK}/localappdata")" pwsh -NoProfile -NonInteractive \
      -ExecutionPolicy Bypass -File "$(cygpath -w "${REPO}/install.ps1")" \
      > "${WORK}/install.out" 2>&1 || code=$?
  else
    HOME="${WORK}/home" sh "${REPO}/install.sh" > "${WORK}/install.out" 2>&1 || code=$?
  fi
  cat "${WORK}/install.out"
  return "${code}"
}

echo "\$ the installer, with a wrong ${ARCHIVE}.sha256"
printf '%s  %s\n' "0000000000000000000000000000000000000000000000000000000000000000" \
  "${ARCHIVE}" > "${RELEASE}/download/${TAG}/${ARCHIVE}.sha256"
if run_installer; then
  fail "the installer took an archive whose checksum doesn't match"
fi
grep -q "checksum mismatch" "${WORK}/install.out" || fail "no checksum mismatch reported"
[ ! -e "${INSTALLED}" ] || fail "a binary was installed after a checksum mismatch"

echo ""
echo "\$ the installer, with the release's ${ARCHIVE}.sha256"
cp "${ASSETS}/${ARCHIVE}.sha256" "${RELEASE}/download/${TAG}/"
run_installer || fail "the installer failed"
grep -q "Checking the checksum" "${WORK}/install.out" || fail "the checksum wasn't checked"
[ -f "${INSTALLED}" ] || fail "no binary at ${INSTALLED}"
# a Windows program ends its lines with CRLF, and Git Bash's $(...) keeps the CR
GOT="$("${INSTALLED}" --version | tr -d '\r')"
[ "${GOT}" = "${VERSION}" ] || fail "the installed binary printed '${GOT}', expected '${VERSION}'"

echo ""
echo "install_check: OK (${ARCHIVE}, ${VERSION})"
