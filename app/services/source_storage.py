"""Private local originals; durable opt-in storage, no migration or automatic deletion."""

from collections.abc import Iterator
from contextlib import contextmanager
from os import (
    O_CREAT,
    O_DIRECTORY,
    O_EXCL,
    O_NOFOLLOW,
    O_RDONLY,
    O_WRONLY,
    close,
    fdopen,
    fstat,
    fsync,
    geteuid,
)
from os import open as open_fd
from pathlib import Path
from stat import S_IMODE, S_ISDIR
from typing import BinaryIO

from app.errors import AppError


def _unavailable() -> AppError:
    return AppError(
        "原件保存目录不可用，请检查本地目录及权限后使用原上传标识重试",
        code="SOURCE_STORAGE_UNAVAILABLE",
        http_status=503,
        retryable=True,
    )


def _check_path(path: Path) -> None:
    if (
        not path.is_absolute()
        or path == Path("/")
        or ".." in path.parts
        or any(part.is_symlink() for part in (path, *path.parents))
    ):
        raise OSError("Invalid source root")


@contextmanager
def _private_directory(path: Path) -> Iterator[int]:
    # Configuration is operator-controlled. Reject aliases and unsafe existing
    # roots rather than changing somebody else's directory permissions.
    descriptor = None
    try:
        _check_path(path)
        descriptor = open_fd(path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
        current = fstat(descriptor)
        if (
            not S_ISDIR(current.st_mode)
            or current.st_uid != geteuid()
            or S_IMODE(current.st_mode) & 0o077
            or S_IMODE(current.st_mode) & 0o700 != 0o700
        ):
            raise OSError("Source root must be private and owned by the process user")
        yield descriptor
    except OSError:
        raise _unavailable() from None
    finally:
        if descriptor is not None:
            close(descriptor)


def prepare_source_storage(path: Path | None) -> Path | None:
    if path is None:
        return None  # Compatibility: temporary roots are created only on upload.
    created = False
    try:
        _check_path(path)
        # Only the leaf is created; parent provisioning remains an operator action.
        try:
            path.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            pass
        if created:
            parent = open_fd(path.parent, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
            try:
                fsync(parent)
            finally:
                close(parent)
    except OSError:
        raise _unavailable() from None
    with _private_directory(path):
        pass
    return path


@contextmanager
def new_original(root: Path, source_id: str) -> Iterator[tuple[Path, BinaryIO]]:
    """Create once; fsync contents and the directory before the DB can commit.

    The caller records its created path after entering, and owns rollback cleanup.
    A crash before DB commit can leave an unreferenced file, never a success receipt.
    """
    with _private_directory(root) as directory:
        descriptor = open_fd(
            source_id, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600, dir_fd=directory
        )
        with fdopen(descriptor, "wb") as stream:
            yield root / source_id, stream
            stream.flush()
            fsync(stream.fileno())
        fsync(directory)
