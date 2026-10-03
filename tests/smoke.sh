#!/bin/sh
# The smoke test of a built binary (DESIGN, "Packaging"): the same on Linux, macOS and Windows
# (Git Bash), on a release runner and on a dev box.
#
#   sh tests/smoke.sh DIST_DIR WORK_DIR [VERSION]
#
# DIST_DIR holds the binary just built (vcharon, or vcharon.exe); WORK_DIR is a scratch folder,
# made if missing: HOME, VCharon's folders and the channel root all go under it, so the run
# touches nothing else on the machine. VERSION is what --version must print (default: the
# package's, from src/vcharon/__init__.py). It needs no ssh; SMOKE_PING=localhost adds
# vcharon ping localhost, for a box whose sshd takes this user's key.
set -eu

if [ $# -lt 2 ]; then
  echo "usage: sh tests/smoke.sh DIST_DIR WORK_DIR [VERSION]" >&2
  exit 3
fi
DIST="$(cd "$1" && pwd)"
mkdir -p "$2"
WORK="$(cd "$2" && pwd)"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
if [ $# -ge 3 ]; then
  VERSION="$3"
else
  VERSION="$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "${REPO}/src/vcharon/__init__.py")"
fi
PYTHON="${PYTHON:-}"
if [ -z "${PYTHON}" ]; then
  if command -v python3 > /dev/null 2>&1 && python3 -c "" > /dev/null 2>&1; then
    PYTHON=python3
  else
    PYTHON=python
  fi
fi

export PATH="${DIST}:${PATH}"
export HOME="${WORK}/home"
export VCHARON_HOME="${WORK}/vcharon-home"
export VCHARON_CHANNELS_ROOT="${WORK}/channels"
# Python's expanduser reads USERPROFILE on Windows (skill install writes under it)
if command -v cygpath > /dev/null 2>&1; then
  USERPROFILE="$(cygpath -w "${HOME}")"
  export USERPROFILE
fi
mkdir -p "${HOME}" "${WORK}/smoke"
# a fresh folder named smoke: the project part of the member's name comes from it
cd "${WORK}/smoke"

step() {
  echo ""
  echo "\$ $*"
}

fail() {
  echo "smoke: FAIL: $*" >&2
  exit 1
}

step vcharon --version
GOT="$(vcharon --version)"
echo "${GOT}"
[ "${GOT}" = "${VERSION}" ] || fail "--version printed '${GOT}', expected '${VERSION}'"

# judged by its fields, not its exit code: a fresh machine has no config, so it may warn
step vcharon doctor --json
vcharon doctor --json > doctor.json || true
"${PYTHON}" - doctor.json <<'PYEOF'
import json, sys
doc = json.load(open(sys.argv[1], encoding="utf-8"))
bundle = doc["helper_bundle"]
print("helper_bundle:", json.dumps(bundle), "install:", json.dumps(doc["install"]))
if not (bundle["has_helper"] and bundle["modules"] > 1):
    sys.exit("the helper's bundle has no vcharon.helper")
if doc["install"]["kind"] != "binary":
    sys.exit("doctor doesn't see a binary: %r" % doc["install"])
PYEOF

step vcharon setup --box ci
vcharon setup --box ci

step vcharon create smoke --local
vcharon create smoke --local

step 'echo hello | vcharon post smoke --to @all --title "smoke test"'
echo hello | vcharon post smoke --to @all --title "smoke test"

step vcharon read smoke --last 1 --json
vcharon read smoke --last 1 --json > read.json
cat read.json
grep -q '"smoke test"' read.json || fail "read --json doesn't hold the entry"

step vcharon guide
vcharon guide > guide.txt
head -n 3 guide.txt
[ "$(wc -l < guide.txt)" -gt 20 ] || fail "vcharon guide printed too little"

step vcharon skill install
vcharon skill install
for agent_dir in .claude .agents; do
  [ -s "${HOME}/${agent_dir}/skills/vcharon/SKILL.md" ] \
    || fail "skill install wrote no ${agent_dir}/skills/vcharon/SKILL.md"
done

if [ -n "${SMOKE_PING:-}" ]; then
  step vcharon ping "${SMOKE_PING}"
  vcharon ping "${SMOKE_PING}"
fi

step vcharon close smoke
vcharon close smoke

echo ""
echo "smoke: OK (${VERSION})"
