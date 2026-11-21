#!/bin/sh
# Isolated hosted CI runner only. Its filesystem and systemd are disposable.
set -eu
test "${GITHUB_ACTIONS:-}" = true
test "${RUNNER_ENVIRONMENT:-}" = github-hosted
test "$(id -u)" = 0
test ! -e /opt/zarya
test ! -e /var/lib/zarya
test ! -e /var/lib/zarya-cd
test ! -e /etc/systemd/system/zarya.service
id zarya-svc 2>/dev/null && exit 1
trap 'if [ "$?" -ne 0 ]; then journalctl --no-pager -u zarya-cd.service -u zarya.service -n 100; fi' EXIT
apt-get install -y python3.12-venv polkitd age >/dev/null
rczip=$(mktemp)
curl -fsSL --retry 3 https://downloads.rclone.org/v1.76.0/rclone-v1.76.0-linux-amd64.zip -o "$rczip"
echo "b00f00b3266c3764413afe599d3ec316f6f43b3e0ea8a0bc1d186d2133e46df7  $rczip" | sha256sum -c -
unzip -p "$rczip" rclone-v1.76.0-linux-amd64/rclone > /usr/local/bin/rclone
chmod 0755 /usr/local/bin/rclone
useradd --system --no-create-home zarya-svc
install -d -m 0755 /opt/zarya /etc/zarya-cd
install -d -m 0700 -o zarya-svc -g zarya-svc /var/lib/zarya
install -m 0600 -o zarya-svc -g zarya-svc src/zarya/prompts.example.json /var/lib/zarya/prompts.json
install -m 0600 /dev/null /etc/zarya-cd/github-token
install -d -m 0700 -o zarya-svc -g zarya-svc /var/lib/zarya-backup
install -d -m 0700 -o zarya-svc -g zarya-svc /var/lib/zarya-backup/remote
age-keygen -o /root/ci-age-identity
age-keygen -y /root/ci-age-identity > /etc/zarya-cd/age-recipient
chmod 0600 /etc/zarya-cd/age-recipient
install -m 0600 -o zarya-svc -g zarya-svc /dev/null /var/lib/zarya-backup/rclone.conf
printf '%s\n' '[zarya-disk]' 'type = yandex' 'app_folder = true' 'client_id = fixture' 'token = fixture' > /var/lib/zarya-backup/rclone.conf
cp -r dist/release /opt/zarya/artifact
/usr/bin/python3.12 -m venv /opt/zarya/.venv
/opt/zarya/.venv/bin/pip -q install --only-binary=:all: --require-hashes -r dist/release/requirements.txt
/opt/zarya/.venv/bin/pip -q install --no-deps dist/release/*.whl
cat > /etc/systemd/system/zarya.service <<'EOF'
[Service]
User=zarya-svc
Group=zarya-svc
WorkingDirectory=/opt/zarya
Environment=ZARYA_DATA_DIR=/var/lib/zarya
ExecStart=/opt/zarya/.venv/bin/python -m zarya
ProtectSystem=strict
ReadWritePaths=/var/lib/zarya
EOF
systemctl daemon-reload
systemctl start zarya.service
curl --fail --retry 20 --retry-connrefused --retry-delay 1 http://127.0.0.1:8787/healthz
sh deploy/install.sh
# Both service units exercise real rclone, age, backup and restore. Only the remote is local.
printf '%s\n' '[zarya-disk]' 'type = alias' 'remote = /var/lib/zarya-backup/remote' > /var/lib/zarya-backup/rclone.conf
systemctl start zarya-backup.service
systemctl start zarya-backup.service
test "$(find /var/lib/zarya-backup/remote/Zarya/v1/nightly -name '*.age' | wc -l)" -eq 1
install -m 0644 tests/systemd_fixture.py /opt/zarya/fixture.py
install -d /etc/systemd/system/zarya-cd.service.d
printf '%s\n' '[Service]' 'ExecStart=' 'ExecStart=/usr/bin/python3 /opt/zarya/fixture.py' \
  > /etc/systemd/system/zarya-cd.service.d/fixture.conf
systemctl daemon-reload
systemctl start zarya-cd.timer
systemctl start zarya-cd.service
systemctl is-active --quiet zarya.service
systemctl is-active --quiet zarya-cd.timer
curl --fail http://127.0.0.1:8787/healthz
for kind in nightly predeploy; do
  blob=$(find "/var/lib/zarya-backup/remote/Zarya/v1/$kind" -name '*.age')
  /opt/zarya/.venv/bin/python deploy/restore_disk.py "$blob" /root/ci-age-identity "/tmp/cd-restore-$kind"
  test -f "/tmp/cd-restore-$kind/restore-quarantine.json"
done
