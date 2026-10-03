#!/bin/sh
# Makes a GitHub Actions Ubuntu runner its own ssh server for the real-ssh checks (DESIGN,
# "Tests"): sshd started, a key without a passphrase made and authorized, localhost in
# known_hosts, then one BatchMode login to prove it. Then `vcharon ping localhost` and
# tests/ssh_flow.sh localhost work there.
#
# The server side runs the python3 an ssh login finds, and the runner's own is older than
# VCharon's floor (Ubuntu 24.04 ships 3.12): so the python3 on this script's PATH, setup-python's,
# is linked as /usr/local/bin/python3, which comes before /usr/bin in sshd's PATH, and a login is
# checked to get 3.13 or later. Run it after setup-python. Its binary finds its libpython through
# its RUNPATH (the tool cache's absolute lib folder), so no LD_LIBRARY_PATH is needed over ssh.
#
#   sh tests/ci_sshd.sh
#
# It changes ~/.ssh, /usr/local/bin and starts a system service, so it runs only on a GitHub
# Actions runner.
set -eu

if [ "${GITHUB_ACTIONS:-}" != true ]; then
  echo "ci_sshd: it changes ~/.ssh and starts sshd; it runs only on a GitHub Actions runner" >&2
  exit 3
fi

# an image may run sshd already, or start it on the first connection (ssh.socket)
if [ -n "$(ssh-keyscan -T 5 localhost 2> /dev/null)" ]; then
  echo "ci_sshd: sshd answers on localhost already"
else
  if [ ! -x /usr/sbin/sshd ]; then
    sudo apt-get update -q
    sudo apt-get install -y -q openssh-server
  fi
  sudo mkdir -p /run/sshd
  sudo systemctl start ssh 2> /dev/null || sudo service ssh start
fi

# sshd's StrictModes refuses a key whose home or ~/.ssh others can write
chmod go-w "${HOME}"
mkdir -p "${HOME}/.ssh"
chmod 700 "${HOME}/.ssh"
if [ ! -f "${HOME}/.ssh/id_ed25519" ]; then
  ssh-keygen -q -t ed25519 -N "" -f "${HOME}/.ssh/id_ed25519"
fi
cat "${HOME}/.ssh/id_ed25519.pub" >> "${HOME}/.ssh/authorized_keys"
chmod 600 "${HOME}/.ssh/authorized_keys"

# sshd may take a moment to listen
tries=0
until ssh-keyscan -T 5 localhost > "${RUNNER_TEMP:-/tmp}/localhost.keys" 2> /dev/null \
    && [ -s "${RUNNER_TEMP:-/tmp}/localhost.keys" ]; do
  tries=$((tries + 1))
  if [ "${tries}" -ge 30 ]; then
    echo "ci_sshd: sshd doesn't answer on localhost" >&2
    exit 1
  fi
  sleep 1
done
cat "${RUNNER_TEMP:-/tmp}/localhost.keys" >> "${HOME}/.ssh/known_hosts"
chmod 600 "${HOME}/.ssh/known_hosts"

ssh -o BatchMode=yes localhost true

# setup-python's 3.13 as the python3 of an ssh login (see the header)
PY="$(python3 -c 'import os, sys; print(os.path.realpath(sys.executable))')"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 13) else 1)' || {
  echo "ci_sshd: python3 on PATH is $(python3 --version 2>&1) (${PY}); run setup-python with" \
    "3.13 or later first" >&2
  exit 1
}
sudo ln -sf "${PY}" /usr/local/bin/python3
if ! ssh -o BatchMode=yes localhost \
    "python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 13) else 1)'"; then
  echo "ci_sshd: an ssh login's python3 isn't 3.13 or later:" \
    "$(ssh -o BatchMode=yes localhost 'command -v python3; python3 --version' 2>&1)" >&2
  exit 1
fi
echo "ci_sshd: OK (ssh localhost works under BatchMode; its python3 is" \
  "$(ssh -o BatchMode=yes localhost 'python3 --version' 2>&1))"
