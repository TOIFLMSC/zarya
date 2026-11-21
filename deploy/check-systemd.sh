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
apt-get install -y python3.12-venv polkitd >/dev/null
useradd --system --no-create-home zarya-svc
install -d -m 0755 /opt/zarya /etc/zarya-cd
install -d -m 0700 -o zarya-svc -g zarya-svc /var/lib/zarya
install -m 0600 /dev/null /etc/zarya-cd/github-token
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
install -m 0644 tests/systemd_fixture.py /opt/zarya/fixture.py
install -d /etc/systemd/system/zarya-cd.service.d
printf '%s\n' '[Service]' 'ExecStart=' 'ExecStart=/usr/bin/python3 /opt/zarya/fixture.py' \
  > /etc/systemd/system/zarya-cd.service.d/fixture.conf
systemctl daemon-reload
systemctl start zarya-cd.timer
systemctl start zarya-cd.service || {
  journalctl --no-pager -u zarya-cd.service -u zarya.service -n 120
  exit 1
}
systemctl is-active --quiet zarya.service
systemctl is-active --quiet zarya-cd.timer
curl --fail http://127.0.0.1:8787/healthz
