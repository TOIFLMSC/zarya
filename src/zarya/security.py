import hashlib
import hmac
import os
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return salt.hex() + ":" + digest.hex()


def verify_password(password: str, stored: str) -> bool:
    salt, digest = stored.split(":")
    actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return hmac.compare_digest(actual.hex(), digest)


def bootstrap_token(path: Path) -> str:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.is_symlink():
            raise ValueError("Invalid bootstrap token file") from None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), encoding="utf-8") as file:
            mode = os.fstat(file.fileno()).st_mode
            if not stat.S_ISREG(mode):
                raise ValueError("Invalid bootstrap token file") from None
            chmod_descriptor = getattr(os, "fchmod", None)
            if chmod_descriptor is not None and stat.S_IMODE(mode) != 0o600:
                chmod_descriptor(file.fileno(), 0o600)
            return file.read().strip()
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        token = secrets.token_urlsafe(32)
        file.write(token)
    return token


@dataclass
class Session:
    csrf: str
    expires: float


class Sessions:
    def __init__(self, lifetime: int):
        self.lifetime = lifetime
        self.values: dict[str, Session] = {}

    def create(self) -> tuple[str, Session]:
        now = time.monotonic()
        self.values = {key: value for key, value in self.values.items() if value.expires > now}
        if len(self.values) >= 64:
            self.values.pop(next(iter(self.values)))
        token = secrets.token_urlsafe(32)
        session = Session(csrf=secrets.token_urlsafe(32), expires=now + self.lifetime)
        self.values[self.key(token)] = session
        return token, session

    @staticmethod
    def key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def get(self, token: str) -> Session | None:
        session = self.values.get(self.key(token))
        if session and session.expires > time.monotonic():
            return session
        self.values.pop(self.key(token), None)
        return None

    def remove(self, token: str) -> None:
        self.values.pop(self.key(token), None)
