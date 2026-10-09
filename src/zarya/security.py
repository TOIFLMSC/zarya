import hashlib
import hmac
import secrets
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
    if not path.exists():
        with path.open("x", encoding="utf-8") as file:
            file.write(secrets.token_urlsafe(32))
        path.chmod(0o600)
    return path.read_text(encoding="utf-8").strip()


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
