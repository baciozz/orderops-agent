"""Bounded readback of an immutable, server-named original source file."""

from hashlib import sha256
from os import O_NOFOLLOW, O_NONBLOCK, O_RDONLY, fdopen, fstat
from os import open as open_fd
from pathlib import Path
from stat import S_ISREG

from app.adapters.uploads import MAX_FILE_BYTES
from app.db.models import InputSource
from app.errors import AppError


def original_content(source: InputSource) -> bytes:
    path = Path(source.storage_ref)
    valid = (
        path.is_absolute()
        and path.name == source.id
        and not path.is_symlink()
        and type(source.size) is int
        and 0 <= source.size <= MAX_FILE_BYTES
    )
    content = b""
    if valid:
        try:
            with fdopen(open_fd(path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK), "rb") as stream:
                if not S_ISREG(fstat(stream.fileno()).st_mode):
                    raise OSError("Source is not a regular file")
                content = stream.read(MAX_FILE_BYTES + 1)
        except OSError:
            valid = False
    if not valid or len(content) != source.size or sha256(content).hexdigest() != source.sha256:
        raise AppError(
            "原始来源文件缺失或已变化，请恢复可信原件或重新创建任务并上传",
            code="SOURCE_UNAVAILABLE",
            http_status=409,
        )
    return content
