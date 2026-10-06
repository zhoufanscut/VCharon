#!/bin/sh
# A real channel over real ssh (DESIGN, "Tests"): two remote members of one channel, on one
# machine, through its own sshd. CI's ssh job runs it on the runner.
#
#   CI=1 sh tests/ssh_flow.sh DEST WORK_DIR
#
# DEST is this machine's sshd as vcharon takes it (localhost, or ssh://localhost:2222); its key
# must work under BatchMode. The channel, flow, is made in the real channel root of DEST's user
# (~/.local/state/vcharon/channels): the server side reads no test variable over ssh. So it runs
# only where that home is a throwaway, such as a CI runner: CI must be set (or
# SSH_FLOW_REAL_HOME=1, on your own word). It refuses a root that already holds a flow, and
# closes the channel at the end. WORK_DIR is a scratch folder, made if missing: both members'
# VCharon folders and the project go under it. HOME stays as it is, for ssh's keys and
# known_hosts. VCHARON is the command that runs vcharon (default: vcharon on the PATH; split on
# spaces, so "python -m vcharon" works).
set -eu

if [ $# -lt 2 ]; then
  echo "usage: CI=1 sh tests/ssh_flow.sh DEST WORK_DIR" >&2
  exit 3
fi
if [ -z "${CI:-}" ] && [ "${SSH_FLOW_REAL_HOME:-}" != 1 ]; then
  echo "ssh_flow: it writes a channel into the real home of DEST's user; set CI=1 where that" \
    "home is a throwaway (or SSH_FLOW_REAL_HOME=1)" >&2
  exit 3
fi
DEST="$1"
mkdir -p "$2"
WORK="$(cd "$2" && pwd)"
VCHARON="${VCHARON:-vcharon}"
CHANNEL=flow
TITLE="over ssh"

# both members are in the same project, on the same machine: their boxes (ci1, ci2) are what
# tells their names apart
mkdir -p "${WORK}/web/.git" "${WORK}/a" "${WORK}/b"
cd "${WORK}/web"

WATCHER=""
cleanup() {
  if [ -n "${WATCHER}" ]; then kill "${WATCHER}" 2>/dev/null || true; fi
}
trap cleanup EXIT

step() {
  echo ""
  echo "\$ $*"
}

fail() {
  echo "ssh_flow: FAIL: $*" >&2
  exit 1
}

# vcharon as member a (the leader) or b; the word splitting of VCHARON is meant
a() {
  # shellcheck disable=SC2086
  VCHARON_HOME="${WORK}/a" ${VCHARON} "$@"
}

b() {
  # shellcheck disable=SC2086
  VCHARON_HOME="${WORK}/b" ${VCHARON} "$@"
}

step a: vcharon ping "${DEST}"
a ping "${DEST}"

# never a channel someone else made: close would delete it
step a: vcharon list --server "${DEST}" --json
a list --server "${DEST}" --json > "${WORK}/list.json"
if grep -q "\"name\": \"${CHANNEL}\"" "${WORK}/list.json"; then
  fail "${DEST} already holds a channel ${CHANNEL}; close it or use another server"
fi

step a: vcharon setup --box ci1
a setup --box ci1
step b: vcharon setup --box ci2
b setup --box ci2

step a: vcharon create "${CHANNEL}" --server "${DEST}"
a create "${CHANNEL}" --server "${DEST}"

step b: vcharon join "${CHANNEL}" --server "${DEST}"
b join "${CHANNEL}" --server "${DEST}"

# b's watcher starts before a's post: it goes on from the snapshot join saved, so only the post
# is new. --max-minutes bounds it if the entry never comes.
step b: vcharon watch "${CHANNEL}" --until-change --max-minutes 2 '&'
b watch "${CHANNEL}" --until-change --max-minutes 2 > "${WORK}/watch.out" 2>&1 &
WATCHER=$!
tries=0
until grep -q "watching" "${WORK}/watch.out"; do
  tries=$((tries + 1))
  if [ "${tries}" -gt 120 ] || ! kill -0 "${WATCHER}" 2>/dev/null; then
    cat "${WORK}/watch.out"
    fail "the watcher didn't start"
  fi
  sleep 0.5
done
cat "${WORK}/watch.out"

step "a: echo hello | vcharon post ${CHANNEL} --to @all --title \"${TITLE}\""
echo hello | a post "${CHANNEL}" --to @all --title "${TITLE}"

step a: vcharon sync "${CHANNEL}"
a sync "${CHANNEL}"

step b: wait for the watcher
code=0
wait "${WATCHER}" || code=$?
WATCHER=""
cat "${WORK}/watch.out"
[ "${code}" -eq 0 ] || fail "the watcher exited ${code}, expected 0 (EXIT change)"
grep -q "to all: .*${TITLE}" "${WORK}/watch.out" || fail "the watcher didn't print the entry"
tail -n 1 "${WORK}/watch.out" | grep -q "EXIT change" \
  || fail "the watcher's last line isn't EXIT change"

step b: vcharon read "${CHANNEL}" --json
b read "${CHANNEL}" --json > "${WORK}/read.json"
cat "${WORK}/read.json"
grep -q "\"${TITLE}\"" "${WORK}/read.json" || fail "read --json doesn't hold the entry"

step b: vcharon leave "${CHANNEL}"
b leave "${CHANNEL}"

step a: vcharon close "${CHANNEL}"
a close "${CHANNEL}"
a list --server "${DEST}" --json > "${WORK}/list.json"
if grep -q "\"name\": \"${CHANNEL}\"" "${WORK}/list.json"; then
  fail "close left ${CHANNEL} on ${DEST}"
fi

echo ""
echo "ssh_flow: OK"
