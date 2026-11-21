"""Install one trusted project adapter and root-managed systemd units."""

import argparse
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

KIT = Path(__file__).resolve().parent


def call(*args):
    return subprocess.check_call(args)


def active(unit):
    return subprocess.check_output(
        ["systemctl", "show", "--value", "-p", "ActiveState", unit], text=True
    ).strip()


def pattern(value, expr, label):
    if not isinstance(value, str) or not re.fullmatch(expr, value):
        raise ValueError(f"invalid {label}")


def limit(value, label):
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} must be a positive integer")


def validate(config):
    name = config["name"]
    pattern(name, r"[a-z][a-z0-9-]{0,19}", "project name")
    github = config["github"]
    for key, expr in (
        ("repository", r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"),
        ("branch", r"[A-Za-z0-9_-][A-Za-z0-9_.-]*"),
        ("workflow", r"[A-Za-z0-9_-]+\.ya?ml"),
        ("prefix", r"[A-Za-z0-9_-]+"),
    ):
        pattern(github[key], expr, key)
    url = urlsplit(config["health_url"])
    if url.scheme != "http" or url.hostname != "127.0.0.1" or url.username or not url.port:
        raise ValueError("health_url must use HTTP on 127.0.0.1 with a dedicated port")
    for key in ("poll_seconds", "health_timeout_seconds", "backup_retry_seconds"):
        limit(config[key], key)
    backup = config["backup"]
    for key in ("keep_nightly", "keep_predeploy"):
        limit(backup[key], key)
    pattern(
        backup["remote"],
        r"[A-Za-z0-9_-]+:(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+",
        "dedicated rclone folder",
    )
    return name


def units(name, user, app, conf, state, code, config):
    shared = f"""Group={user}
UMask=0007
Environment=PYTHONDONTWRITEBYTECODE=1
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
NoNewPrivileges=true
"""
    credential = f"LoadCredential=age-recipient:{conf}/age-recipient\n"
    result = {}
    for kind, action in (("app", "start"), ("deploy", "poll"), ("backup", "nightly")):
        opts = shared + f"User={app if action == 'start' else user}\n"
        if action == "start":
            opts += "Restart=on-failure\nRestartPreventExitStatus=75\n"
            opts += f"ReadWritePaths={state}/data\nInaccessiblePaths={state}/storage\n"
        else:
            opts += f"ReadWritePaths={state}\n"
            opts += f"Type=oneshot\nTimeoutStartSec={'1h' if action == 'nightly' else '30min'}\n"
            opts += credential
            if action == "poll":
                opts += f"LoadCredential=github-token:{conf}/github-token\n"
        result[f"pull-cd-{kind}@{name}.service"] = (
            f"[Unit]\nDescription=Pull CD {name} {action}\n"
            "After=network-online.target\nWants=network-online.target\n"
            f"[Service]\n{opts}ExecStart=/usr/bin/python3 -B {code}/controller.py "
            f"{conf}/project.json {action}\n"
            + ("[Install]\nWantedBy=multi-user.target\n" if action == "start" else "")
        )
    result[f"pull-cd-deploy@{name}.timer"] = f"""[Unit]
Description=Check verified releases for {name}
[Timer]
OnBootSec=45s
OnUnitInactiveSec={config["poll_seconds"]}s
AccuracySec=1s
[Install]
WantedBy=timers.target
"""
    result[f"pull-cd-backup@{name}.timer"] = f"""[Unit]
Description=Capture or retry a daily backup for {name}
[Timer]
OnCalendar=*-*-* *:17:00 UTC
Persistent=true
[Install]
WantedBy=timers.target
"""
    return result


def check_secret(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError(f"{path} must be a regular file owned by root with mode 0600")
    if not info.st_size:
        raise ValueError(f"{path} is empty")


def root_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError(f"{path} must be a protected root-owned directory")


def install(config_path, adapter_path, storage_path):
    if os.geteuid() != 0:
        raise SystemExit("run installer as root")
    config = json.loads(config_path.read_text())
    name = validate(config)
    user = f"pcd-{name}"
    app = f"pca-{name}"
    stem = f"pull-cd-{name}"
    conf = Path("/etc/pull-cd") / name
    state = Path("/var/lib") / stem
    code = Path("/usr/local/lib/pull-cd") / name
    for command in ("age", "rclone", "systemctl", "systemd-analyze", "useradd"):
        if shutil.which(command) is None:
            raise ValueError(f"required program missing: {command}")
    if not Path("/etc/polkit-1/rules.d").is_dir():
        raise ValueError("polkit rules directory missing")
    root_directory(conf.parent)
    root_directory(conf)
    installed = (conf / "project.json").is_file()
    files = units(name, user, app, conf, state, code, config)
    for unit in files:
        target = Path("/etc/systemd/system") / unit
        if not installed and (target.exists() or target.is_symlink()):
            raise ValueError(f"unit {unit} already exists")
        if unit == f"pull-cd-app@{name}.service" and installed:
            continue
        if active(unit) not in {"inactive", "failed"}:
            raise ValueError(f"wait until {unit} is inactive")
    for secret in (conf / "github-token", conf / "age-recipient", storage_path):
        check_secret(secret)
    try:
        pwd.getpwnam(user)
        if not (conf / "project.json").is_file():
            raise ValueError(f"service user {user} already exists")
    except KeyError:
        call(
            "useradd",
            "--system",
            "--user-group",
            "--no-create-home",
            "--shell",
            "/usr/sbin/nologin",
            user,
        )
    try:
        pwd.getpwnam(app)
        if not installed:
            raise ValueError(f"app user {app} already exists")
    except KeyError:
        call(
            "useradd",
            "--system",
            "-g",
            user,
            "--no-create-home",
            "--shell",
            "/usr/sbin/nologin",
            app,
        )
    call("chmod", "0755", conf)
    call("install", "-d", "-m", "0755", code)
    call("install", "-d", "-m", "0750", "-o", user, "-g", user, state, state / "releases")
    call("install", "-d", "-m", "2770", "-o", user, "-g", user, state / "data")
    call("install", "-d", "-m", "0700", "-o", user, "-g", user, state / "storage")
    storage = state / "storage/rclone.conf"
    call("install", "-m", "0600", "-o", user, "-g", user, storage_path, storage)
    config["backup"]["rclone_config"] = str(storage)
    (conf / "project.json").write_text(json.dumps(config, indent=2) + "\n")
    call("chmod", "0644", conf / "project.json")
    call("install", "-m", "0644", adapter_path, conf / "adapter.py")
    for filename in ("backup.py", "controller.py", "pull_release.py"):
        call("install", "-m", "0644", KIT / filename, code / filename)
    for filename, contents in files.items():
        target = Path("/etc/systemd/system") / filename
        target.write_text(contents)
        target.chmod(0o644)
    rules = Path(f"/etc/polkit-1/rules.d/50-{stem}.rules")
    rules.write_text(f"""polkit.addRule(function(action, subject) {{
    if (subject.user == "{user}" &&
        action.id == "org.freedesktop.systemd1.manage-units" &&
        action.lookup("unit") == "pull-cd-app@{name}.service" &&
        ["start", "stop"].indexOf(action.lookup("verb")) >= 0) {{
        return polkit.Result.YES;
    }}
}});
""")
    rules.chmod(0o644)
    call("systemctl", "daemon-reload")
    call("systemd-analyze", "verify", *files)
    print(f"Installed {stem}. Start with a reviewed deployment and restore drill.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("config", "adapter", "rclone_config"):
        parser.add_argument(field, type=Path)
    args = parser.parse_args()
    install(args.config, args.adapter, args.rclone_config)
