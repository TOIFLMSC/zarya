"""Publish verified Zarya snapshots as age-encrypted objects on Yandex Disk."""

import hashlib
import json
import os
import re
import subprocess
import tarfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath, PureWindowsPath

REMOTE = "zarya-disk:Zarya/v1"
CONFIG = Path("/var/lib/zarya-backup/rclone.conf")
OBJECT = re.compile(r"(\d{8}T\d{6}Z)-[0-9a-f]{32}-[0-9a-f]{64}\.tar\.gz\.age")


def new_id():
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex


def member(name):
    p = PurePosixPath(name)
    if (
        p.is_absolute()
        or p.as_posix() != name
        or "\\" in name
        or ":" in name
        or any(
            part in {"..", "."} or part.rstrip(" .") != part or PureWindowsPath(part).is_reserved()
            for part in p.parts
        )
    ):
        raise ValueError("invalid snapshot path")
    return name in {"zarya.sqlite3", "prompts.json", "manifest.json"} or (
        len(p.parts) >= 2 and p.parts[0] in {"photos", "media"}
    )


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def snapshot_files(snapshot):
    path = snapshot / "manifest.json"
    if path.resolve() != path.absolute() or not path.is_file():
        raise ValueError("invalid backup manifest path")
    if path.stat().st_size > 8_000_000:
        raise ValueError("backup manifest too large")
    try:
        manifest = json.loads(path.read_bytes())
    except UnicodeError as error:
        raise ValueError("invalid backup manifest encoding") from error
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if (
        not isinstance(files, dict)
        or manifest.get("format") != 1
        or not isinstance(manifest.get("created_at"), str)
        or "zarya.sqlite3" not in files
        or len({name.casefold() for name in files}) != len(files)
    ):
        raise ValueError("invalid backup manifest")
    for name, evidence in files.items():
        if (
            name == "manifest.json"
            or not member(name)
            or not isinstance(evidence, dict)
            or type(evidence.get("size")) is not int
            or evidence["size"] < 0
            or not isinstance(evidence.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", evidence["sha256"])
        ):
            raise ValueError("invalid backup manifest member")
    return files


def seal(snapshot, output, recipient):
    if not re.fullmatch(r"age1[023456789acdefghjklmnpqrstuvwxyz]{58}", recipient):
        raise ValueError("expected one public X25519 age recipient")
    files = snapshot_files(snapshot)
    partial = output.with_suffix(".partial")
    partial.unlink(missing_ok=True)
    try:
        with partial.open("xb") as dest:
            with subprocess.Popen(
                ["age", "-r", recipient], stdin=subprocess.PIPE, stdout=dest
            ) as proc:
                with proc.stdin as pipe, tarfile.open(fileobj=pipe, mode="w|gz") as archive:
                    for name in ["manifest.json", *files]:
                        path = snapshot / name
                        if path.resolve() != path.absolute() or not path.is_file():
                            raise ValueError("unexpected backup member")
                        if name != "manifest.json" and (
                            path.stat().st_size != files[name]["size"]
                            or sha256(path) != files[name]["sha256"]
                        ):
                            raise ValueError("snapshot checksum mismatch")
                        archive.add(path, arcname=name, recursive=False)
                if proc.wait():
                    raise RuntimeError("age encryption failed")
            dest.flush()
            os.fsync(dest.fileno())
        partial.replace(output)
    finally:
        partial.unlink(missing_ok=True)


def rclone(*args):
    result = subprocess.run(
        [
            "rclone",
            "--config",
            str(CONFIG),
            "--contimeout",
            "8s",
            "--timeout",
            "25s",
            "--retries",
            "1",
            "--low-level-retries",
            "1",
            *args,
        ],
        capture_output=True,
        text=True,
        # rclone bounds connection and I/O stalls. systemd bounds the whole job.
        timeout=None if args[0] in {"copyto", "hashsum", "moveto"} else 180,
    )
    if result.returncode:
        error = result.stderr.lower()
        reason = (
            "storage quota"
            if any(s in error for s in ("507", "insufficientstorage", "quota"))
            else (
                "access denied" if any(s in error for s in ("401", "403")) else "transport failure"
            )
        )
        raise RuntimeError(f"backup {args[0]} failed: {reason} (exit {result.returncode})")
    return result.stdout


def listing(kind):
    if kind not in {"nightly", "predeploy"}:
        raise ValueError("unknown backup class")
    folder = f"{REMOTE}/{kind}"
    rclone("mkdir", folder)
    return folder, rclone("lsf", "--files-only", "--max-depth", "1", folder).splitlines()


def publish(snapshot, spool, kind, stamp):
    if not re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{32}", stamp):
        raise ValueError("invalid snapshot identifier")
    recipient = (Path(os.environ["CREDENTIALS_DIRECTORY"]) / "age-recipient").read_text().strip()
    # A single rename commits both the ciphertext and its digest. A crash before
    # that rename leaves only scratch, so retry can seal the verified snapshot again.
    blobs = list(spool.glob("snapshot-*.age"))
    if len(blobs) == 1 and blobs[0].name == f"snapshot-{sha256(blobs[0])}.age":
        blob = blobs[0]
        digest = blob.stem.removeprefix("snapshot-")
    else:
        scratch = spool / "snapshot.age"
        seal(snapshot, scratch, recipient)
        digest = sha256(scratch)
        blob = spool / f"snapshot-{digest}.age"
        scratch.replace(blob)
        for old in blobs:
            if old != blob:
                old.unlink()
        if os.name == "posix":
            fd = os.open(spool, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    name = f"{stamp}-{digest}.tar.gz.age"
    folder, entries = listing(kind)
    final = f"{folder}/{name}"
    # Only finalized, verified objects count towards retention.
    incoming = f"{folder}/upload-{stamp}.partial"
    if name not in entries:
        rclone("copyto", str(blob), incoming, "--ignore-times")
        verify_remote(incoming, digest)
        rclone("moveto", incoming, final, "--immutable")
    verify_remote(final, digest)
    print("Verified encrypted backup", kind, name, flush=True)
    return name


def verify_remote(path, digest):
    result = rclone("hashsum", "SHA-256", "--download", path).split()
    if len(result) != 2 or result[0].lower() != digest:
        raise ValueError("remote ciphertext SHA-256 mismatch")


def prune(kind, now):
    folder, entries = listing(kind)
    owned = sorted(n for n in entries if OBJECT.fullmatch(n))
    if kind == "nightly":
        cutoff = (now - timedelta(days=30)).strftime("%Y%m%dT%H%M%SZ")
        expired = [n for n in owned[:-1] if n[:16] < cutoff]
    else:
        expired = owned[:-10]
        # Called under deploy.lock before a transfer or from idle. A pre-deploy
        # attempt never resumes an old upload after the live service restarts.
        expired.extend(
            n for n in entries if re.fullmatch(r"upload-\d{8}T\d{6}Z-[0-9a-f]{32}\.partial", n)
        )
    for name in expired:
        rclone("deletefile", f"{folder}/{name}", "--yandex-hard-delete")
