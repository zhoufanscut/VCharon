#!/bin/sh
# Makes a GitHub Actions Ubuntu runner its own ssh server for the real-ssh checks (DESIGN,
# "Tests"): sshd started, a key without a passphrase made and authorized, localhost in
# known_hosts, then one BatchMode login to prove it. Then `vcharon ping localhost` and
# tests/ssh_flow.sh localhost work there.
#
#   sh tests/ci_sshd.sh
#
# It changes ~/.ssh and starts a system service, so it runs only on a GitHub Actions runner.
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
echo "ci_sshd: OK (ssh localhost works under BatchMode)"
