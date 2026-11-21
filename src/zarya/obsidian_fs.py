"""Bounded local files; Windows exclusive file handles also exclude Sync writes."""

import ctypes
import hashlib
import os
import stat
import sys
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import BinaryIO

MAX_FILE = 128_000


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def no_link(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("Ссылки и junction в папке синхронизации запрещены")


@contextmanager
def parents(root: Path, relative: str, *, create: bool = False) -> Iterator[Path]:
    rel = Path(relative)
    if rel.is_absolute() or not rel.parts or any(p in {"..", "."} for p in rel.parts):
        raise ValueError("Недопустимый путь заметки")
    if any(":" in p for p in rel.parts):
        raise ValueError("Недопустимый путь заметки")
    handles: list[int] = []
    try:
        parts = list(reversed(root.absolute().parents)) + [root.absolute()]
        for item in parts + [root.joinpath(*rel.parts[:i]) for i in range(1, len(rel.parts))]:
            if create and not item.exists():
                item.mkdir()
            no_link(item)
            if not item.is_dir():
                raise ValueError("Путь синхронизации не является папкой")
            if sys.platform == "win32":
                # Holding each parent without FILE_SHARE_DELETE prevents directory replacement.
                # Metadata-only access (0) does not enforce Windows delete sharing.
                handles.append(win_open(item, 1, 3, 0x02000000 | 0x00200000, 3))
        yield root / rel
    finally:
        for handle in reversed(handles):
            win_close(handle)


def win_open(path: Path, access: int, disposition: int, flags: int, share: int = 0) -> int:
    if sys.platform != "win32":
        raise OSError("Windows file handles require Windows")
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = kernel.CreateFileW
    fn.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    fn.restype = wintypes.HANDLE
    handle = fn(str(path), access, share, None, disposition, flags, None)
    if handle == ctypes.c_void_p(-1).value:
        code = ctypes.get_last_error()
        if code in {2, 3}:
            raise FileNotFoundError(str(path))
        if code in {80, 183}:
            raise FileExistsError(str(path))
        raise OSError(code, "Файл занят или недоступен")

    # The opened object, not a second path lookup, must not be a reparse point.
    class Info(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]

    info = Info()
    check = kernel.GetFileInformationByHandleEx
    check.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    check.restype = wintypes.BOOL
    if (
        not check(handle, 9, ctypes.byref(info), ctypes.sizeof(info))
        or info.attributes & 0x400
        or (flags & 0x02000000 and not info.attributes & 0x10)
    ):
        win_close(handle)
        raise ValueError("Недопустимый объект файловой системы")
    return int(handle)


def win_close(handle: int) -> None:
    if sys.platform != "win32":
        raise OSError("Windows file handles require Windows")
    from ctypes import wintypes

    fn = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    fn.argtypes = [wintypes.HANDLE]
    fn.restype = wintypes.BOOL
    fn(handle)


@contextmanager
def opened(path: Path, *, create: bool = False, write: bool = False) -> Iterator[BinaryIO]:
    if sys.platform == "win32":
        import msvcrt

        handle = win_open(
            path, 0x80000000 | (0x40000000 if write else 0), 1 if create else 3, 0x00200000
        )
        fd = msvcrt.open_osfhandle(handle, os.O_BINARY | (os.O_RDWR if write else os.O_RDONLY))
    else:
        fd = os.open(
            path,
            (os.O_RDWR if write else os.O_RDONLY)
            | getattr(os, "O_NOFOLLOW", 0)
            | (os.O_CREAT | os.O_EXCL if create else 0),
            0o600,
        )
    with os.fdopen(fd, "r+b" if write else "rb") as stream:
        yield stream


def read(root: Path, name: str, *, limit: int = MAX_FILE) -> bytes | None:
    try:
        with parents(root, name) as path:
            with opened(path) as f:
                data = f.read(limit + 1)
                if len(data) > limit:
                    raise ValueError("Заметка превышает лимит 128 КБ")
                return data
    except FileNotFoundError:
        return None


def write(
    root: Path, name: str, data: bytes, expected: str | None, *, limit: int = MAX_FILE
) -> bool:
    """Compare under an exclusive handle. Never replace a file edited since the scan."""
    if len(data) > limit:
        raise ValueError("Заметка превышает лимит 128 КБ")
    with parents(root, name, create=True) as path:
        try:
            with opened(path, create=expected is None, write=True) as f:
                if expected is not None and digest(f.read(limit + 1)) != expected:
                    return False
                f.seek(0)
                f.write(data)
                f.truncate()
                f.flush()
                os.fsync(f.fileno())
                return True
        except FileExistsError:
            return False
        except OSError as exc:
            if expected is None and (path.exists() or exc.errno == 80):
                return False
            raise


def remove(root: Path, name: str) -> None:
    """Only registered owned notes; privacy cleanup includes changed copies."""
    try:
        with parents(root, name) as path:
            no_link(path)
            path.unlink()
    except FileNotFoundError:
        pass


class Reservation:
    """CREATE_NEW reserves ownership before SQL intent; no private bytes until commit."""

    def __init__(self, root: Path, name: str, *, limit: int = MAX_FILE):
        self.limit = limit
        self.stack = ExitStack()
        try:
            path = self.stack.enter_context(parents(root, name, create=True))
            self.stream = self.stack.enter_context(opened(path, create=True, write=True))
        except BaseException:
            self.stack.close()
            raise

    def publish(self, data: bytes) -> None:
        if len(data) > self.limit:
            raise ValueError("Заметка превышает лимит 128 КБ")
        self.stream.write(data)
        self.stream.flush()
        os.fsync(self.stream.fileno())

    def close(self) -> None:
        self.stack.close()
