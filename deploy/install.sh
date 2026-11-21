#!/bin/sh
# Run once as root from the reviewed checkout on the existing Ubuntu VPS.
set -eu
test "$(id -u)" = 0
cd "$(dirname "$0")"
test -x /opt/zarya/.venv/bin/python
test -f /var/lib/zarya/zarya.sqlite3
test "$(systemctl show -p User --value zarya.service)" = zarya-svc
test -f /etc/zarya-cd/github-token
test "$(stat -c '%u:%a' /etc/zarya-cd/github-token)" = 0:600
test ! -e /var/lib/zarya-cd/state.json
test ! -e /etc/systemd/system/zarya.service.d/80-pull-cd.conf
test -d /etc/polkit-1/rules.d
install -d -m 0755 /usr/local/lib/zarya-cd /etc/systemd/system/zarya.service.d
install -m 0644 deployer.py pull_release.py sqlite_release.py /usr/local/lib/zarya-cd/
install -d -o zarya-svc -g zarya-svc -m 0700 /var/lib/zarya-cd \
  /var/lib/zarya-cd/releases /var/lib/zarya-cd/runs
runuser -u zarya-svc -- /usr/bin/python3 -c \
  "import sys; sys.path.insert(0, '/usr/local/lib/zarya-cd'); import deployer; deployer.save({}, 'idle', live=None)"
install -m 0644 80-pull-cd.conf /etc/systemd/system/zarya.service.d/
install -m 0644 zarya-cd.service zarya-cd.timer /etc/systemd/system/
install -m 0644 50-zarya-cd.rules /etc/polkit-1/rules.d/
systemctl daemon-reload
systemd-analyze verify zarya.service zarya-cd.service zarya-cd.timer
echo 'Installed. Enable with systemctl enable --now zarya-cd.timer'
