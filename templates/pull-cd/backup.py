"""Encrypt, deliver, and recover an adapter-owned snapshot without starting it."""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

ID = r"\d{8}T\d{6}Z-[a-f0-9]{32}"
OBJECT = re.compile(ID + r"-(?P<checksum>[a-f0-9]{64})\.tar\.gz\.age")


def new_id():
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def path_for(name):
    posix = PurePosixPath(name)
    if (
        posix.is_absolute()
        or posix.as_posix() != name
        or "\\" in name
        or ":" in name
        or not posix.parts
        or posix.parts[0] != "data"
        or len(posix.parts) < 2
        or any(
            item == ".." or item.rstrip(" .") != item or PureWindowsPath(item).is_reserved()
            for item in posix.parts
        )
    ):
        raise ValueError("unsafe archive member")
    return posix


def index(source):
    entries = {}
    folded = set()
    for path in sorted(source.rglob("*")):
        if path.is_symlink() or path.is_junction() or not (path.is_dir() or path.is_file()):
            raise ValueError("snapshot includes a link or special file")
        name = "data/" + path.relative_to(source).as_posix()
        path_for(name)
        if name.casefold() in folded:
            raise ValueError("duplicate case-insensitive snapshot path")
        folded.add(name.casefold())
        entries[name] = (
            {"type": "directory"}
            if path.is_dir()
            else {"type": "file", "sha256": digest(path), "size": path.stat().st_size}
        )
    return entries


def seal(source, output, recipient):
    key = recipient.read_text().strip()
    if not re.fullmatch(r"age1[023456789acdefghjklmnpqrstuvwxyz]{58}", key):
        raise ValueError("expected one X25519 age recipient")
    files = index(source)
    manifest = json.dumps({"format": 1, "files": files}).encode()
    if len(manifest) > 8_000_000:
        raise ValueError("snapshot manifest too large")
    with output.open("wb") as target:
        with subprocess.Popen(["age", "-r", key], stdin=subprocess.PIPE, stdout=target) as child:
            with child.stdin as pipe, tarfile.open(fileobj=pipe, mode="w|gz") as tar:
                record = tarfile.TarInfo("manifest.json")
                record.size = len(manifest)
                tar.addfile(record, io.BytesIO(manifest))
                for name, metadata in files.items():
                    record = tarfile.TarInfo(name)
                    if metadata["type"] == "directory":
                        record.type = tarfile.DIRTYPE
                        record.mode = 0o700
                        tar.addfile(record)
                        continue
                    record.size = metadata["size"]
                    record.mode = 0o600
                    path = source.joinpath(*path_for(name).parts[1:])
                    with path.open("rb") as stream:
                        tar.addfile(record, stream)
            if child.wait():
                raise RuntimeError("age encryption failed")
        target.flush()
        os.fsync(target.fileno())


def rclone(config, *args):
    result = subprocess.run(
        [
            "rclone",
            "--config",
            config,
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
        timeout=None if args[0] in {"copyto", "moveto", "hashsum"} else 180,
    )
    if result.returncode:
        raise RuntimeError(f"rclone {args[0]} failed ({result.returncode})")
    return result.stdout


def verify_remote(config, path, expected):
    answer = rclone(config, "hashsum", "SHA-256", "--download", path).split()
    if len(answer) != 2 or answer[0].lower() != expected:
        raise ValueError("remote ciphertext SHA-256 mismatch")


def publish(source, spool, remote, stamp, recipient, config, keep):
    if not re.fullmatch(ID, stamp):
        raise ValueError("invalid backup ID")
    blobs = list(spool.glob("snapshot-*.age"))
    if blobs:
        (blob,) = blobs
        checksum = digest(blob)
        if blob.name != f"snapshot-{checksum}.age":
            raise ValueError("cached ciphertext is corrupt")
    else:
        partial = spool / "snapshot.partial"
        seal(source, partial, recipient)
        checksum = digest(partial)
        blob = spool / f"snapshot-{checksum}.age"
        partial.replace(blob)
        subprocess.run(["sync", "-f", spool], check=True)
    name = f"{stamp}-{checksum}.tar.gz.age"
    rclone(config, "mkdir", remote)
    names = rclone(config, "lsf", "--files-only", "--max-depth", "1", remote).splitlines()
    if name not in names:
        incoming = f"{remote}/upload-{stamp}.partial"
        rclone(config, "copyto", str(blob), incoming, "--ignore-times")
        verify_remote(config, incoming, checksum)
        rclone(config, "moveto", incoming, f"{remote}/{name}", "--immutable")
    verify_remote(config, f"{remote}/{name}", checksum)
    retained = sorted(set(n for n in [*names, name] if OBJECT.fullmatch(n)))
    for obsolete in retained[:-keep]:
        # Preserve this successful upload if an operator's clock is behind.
        if obsolete != name:
            rclone(config, "deletefile", f"{remote}/{obsolete}")
    print("verified backup", remote, name, flush=True)
    return name


def recover(archive, identity, target):
    if target.exists() or target.is_symlink():
        raise ValueError("restore requires a new destination")
    match = OBJECT.fullmatch(archive.name)
    if match and digest(archive) != match["checksum"]:
        raise ValueError("download checksum mismatch")
    with tempfile.TemporaryDirectory(dir=target.parent, prefix=".restore-") as temp:
        work = Path(temp)
        decrypted = work / "archive.tar.gz"
        subprocess.run(["age", "-d", "-i", identity, "-o", decrypted, archive], check=True)
        data = work / "data"
        data.mkdir(mode=0o700)
        seen = set()
        with tarfile.open(decrypted, "r:gz") as tar:
            record = tar.next()
            if (
                record is None
                or record.name != "manifest.json"
                or not record.isreg()
                or record.size > 8_000_000
            ):
                raise ValueError("invalid snapshot manifest")
            manifest = json.load(tar.extractfile(record))
            if manifest.get("format") != 1 or not isinstance(manifest.get("files"), dict):
                raise ValueError("unsupported backup format")
            while entry := tar.next():
                name = path_for(entry.name)
                evidence = manifest["files"].get(entry.name)
                if entry.name.casefold() in seen or not isinstance(evidence, dict):
                    raise ValueError("unexpected snapshot member")
                seen.add(entry.name.casefold())
                dest = work.joinpath(*name.parts)
                if entry.isdir() and evidence == {"type": "directory"}:
                    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
                    continue
                if (
                    not entry.isreg()
                    or evidence.get("type") != "file"
                    or evidence.get("size") != entry.size
                    or entry.size > shutil.disk_usage(work).free
                ):
                    raise ValueError("unexpected or oversized snapshot file")
                dest.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(entry) as stream, dest.open("xb") as output:
                    shutil.copyfileobj(stream, output)
                if digest(dest) != evidence.get("sha256"):
                    raise ValueError("snapshot file checksum mismatch")
        if index(data) != manifest["files"]:
            raise ValueError("snapshot manifest inventory mismatch")
        (data / ".restore-quarantine").write_text("Restore for verification only.\n")
        data.rename(target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ("archive", "identity", "target"):
        parser.add_argument(argument, type=Path)
    args = parser.parse_args()
    recover(args.archive, args.identity, args.target)
