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
test "$(stat -c '%u:%a' /etc/zarya-cd/age-recipient)" = 0:600
test "$(stat -c '%U:%a' /var/lib/zarya-backup)" = zarya-svc:700
test "$(stat -c '%U:%a' /var/lib/zarya-backup/rclone.conf)" = zarya-svc:600
age -R /etc/zarya-cd/age-recipient </dev/null >/dev/null
rclone help backend yandex | grep -q -- '--yandex-app-folder'
python3 -c "import configparser; c=configparser.RawConfigParser(); c.read('/var/lib/zarya-backup/rclone.conf'); r=c['zarya-disk']; assert r['type']=='yandex' and r.getboolean('app_folder') and r['client_id'] and r['token']"
for u in zarya-cd.timer zarya-backup.timer zarya-cd.service zarya-backup.service; do
  case "$(systemctl show -p ActiveState --value "$u")" in inactive|failed) ;; *) exit 1 ;; esac
done
test -d /etc/polkit-1/rules.d
if test -e /var/lib/zarya-cd/state.json; then
  runuser -u zarya-svc -- python3 -c "import json; assert json.load(open('/var/lib/zarya-cd/state.json'))['phase']=='idle'"
else
  test ! -e /etc/systemd/system/zarya.service.d/80-pull-cd.conf
fi
install -d -m 0755 /usr/local/lib/zarya-cd /etc/systemd/system/zarya.service.d
for script in disk_backup.py pull_release.py sqlite_release.py deployer.py; do
  install -m 0644 "$script" "/usr/local/lib/zarya-cd/$script.next"
  mv "/usr/local/lib/zarya-cd/$script.next" "/usr/local/lib/zarya-cd/$script"
done
install -d -o zarya-svc -g zarya-svc -m 0700 /var/lib/zarya-cd \
  /var/lib/zarya-cd/releases /var/lib/zarya-cd/runs
if test ! -e /var/lib/zarya-cd/state.json; then
  runuser -u zarya-svc -- /usr/bin/python3 -c \
    "import sys; sys.path.insert(0, '/usr/local/lib/zarya-cd'); import deployer; deployer.save({}, 'idle', live=None)"
fi
install -m 0644 80-pull-cd.conf /etc/systemd/system/zarya.service.d/
install -m 0644 zarya-cd.service zarya-cd.timer zarya-backup.service zarya-backup.timer /etc/systemd/system/
install -m 0644 50-zarya-cd.rules /etc/polkit-1/rules.d/
systemctl daemon-reload
systemd-analyze verify zarya.service zarya-cd.service zarya-cd.timer zarya-backup.service zarya-backup.timer
echo 'After a verified restore, enable with systemctl enable --now zarya-cd.timer zarya-backup.timer'
