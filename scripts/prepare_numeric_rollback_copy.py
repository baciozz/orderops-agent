#!/usr/bin/env python3
"""Prepare a separate SQLite copy for the immediately preceding OCR version.

Stop application writes and back up the SQLite database and original-file
directory first. This tool never modifies the source or replaces a deployment.
"""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
from contextlib import ExitStack, closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

NUMERIC_FIELDS = ("ocr_numeric_check", "ocr_numeric_low_count", "ocr_numeric_low_regions")
MAX_BYTES = 5 * 1024 * 1024


def _private_directory(path: Path) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise ValueError("destination directory must exist and must not be a symlink")
    resolved = path.resolve(strict=True)
    directory_stat = resolved.stat()
    if directory_stat.st_uid != os.geteuid() or stat.S_IMODE(directory_stat.st_mode) & 0o077:
        raise ValueError("destination directory must be private to the current user")
    return resolved


def _source_state(path: Path, *, required: bool = False) -> tuple[int, ...] | None:
    try:
        entry = path.lstat()
    except FileNotFoundError:
        if required:
            raise ValueError("source database is missing") from None
        return None
    if not stat.S_ISREG(entry.st_mode):
        raise ValueError("source database or journal is not a regular file")
    return entry.st_dev, entry.st_ino, entry.st_size, entry.st_mtime_ns, entry.st_ctime_ns


def _snapshot_source(source: Path, directory: Path) -> Path:
    """Read the stopped DB and WAL without opening SQLite on the source files."""
    wal = Path(str(source) + "-wal")
    journal = Path(str(source) + "-journal")
    before_db = _source_state(source, required=True)
    before_wal = _source_state(wal)
    if _source_state(journal) is not None:
        raise ValueError("rollback journal exists; create a clean offline backup first")
    snapshot = directory / "source.sqlite"
    for src, dst, expected in (
        (source, snapshot, before_db),
        (wal, Path(str(snapshot) + "-wal"), before_wal),
    ):
        if expected is None:
            continue
        read_fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(read_fd, "rb") as input_stream:
            entry = os.fstat(input_stream.fileno())
            current = (
                entry.st_dev,
                entry.st_ino,
                entry.st_size,
                entry.st_mtime_ns,
                entry.st_ctime_ns,
            )
            if not stat.S_ISREG(entry.st_mode) or current != expected:
                raise ValueError("source changed during backup; stop writes and retry")
            write_fd = os.open(dst, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(write_fd, "wb") as output_stream:
                shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
    if (
        _source_state(source, required=True) != before_db
        or _source_state(wal) != before_wal
        or _source_state(journal) is not None
    ):
        raise ValueError("source changed during backup; stop writes and retry")
    return snapshot


def _read_source(spec: tuple[str, str, int, str]) -> bytes:
    ref, source_id, size, digest = spec
    if not Path(ref).is_absolute() or Path(ref).name != source_id or not 0 <= size <= MAX_BYTES:
        raise ValueError("invalid original or preview reference")
    read_fd = os.open(ref, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(read_fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("original or preview is not a regular file")
        content = stream.read(MAX_BYTES + 1)
    if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("original or preview failed its saved size or hash")
    return content


def _source_spec(asset: dict[str, Any], prefix: str) -> tuple[str, str, int, str]:
    ref = asset.get(prefix + "path")
    source_id = asset.get(prefix + "storage_id")
    size = asset.get(prefix + "size")
    digest = asset.get(prefix + "sha256")
    if (
        not isinstance(ref, str)
        or not isinstance(source_id, str)
        or type(size) is not int
        or not isinstance(digest, str)
    ):
        raise ValueError("invalid original or preview reference")
    return ref, source_id, size, digest


def _validate_marker(record: Any) -> None:
    if not isinstance(record, dict) or type(record.get("previous_checked_present")) is not bool:
        raise ValueError("invalid rollback audit record")
    if "previous_checked" not in record:
        raise ValueError("invalid rollback audit record")
    if record["previous_checked_present"]:
        if type(record["previous_checked"]) is not bool:
            raise ValueError("invalid rollback audit record")
    elif record["previous_checked"] is not None:
        raise ValueError("invalid rollback audit record")


def _reset_image_review(asset: dict[str, Any]) -> None:
    checked_present = "numeric_fields_checked" in asset
    checked = asset.get("numeric_fields_checked")
    if checked_present and type(checked) is not bool:
        raise ValueError("invalid numeric review declaration")
    marker = asset.get("ocr_numeric_rollback")
    history = asset.get("ocr_numeric_rollback_history")
    if marker is None:
        if "ocr_numeric_rollback" in asset or "ocr_numeric_rollback_history" in asset:
            raise ValueError("invalid rollback audit history")
        records: list[dict[str, Any]] = []
    else:
        _validate_marker(marker)
        if "ocr_numeric_rollback_history" not in asset:
            # The first version kept only the most recent record.
            records = [marker]
        elif not isinstance(history, list) or not history or history[-1] != marker:
            raise ValueError("invalid rollback audit history")
        else:
            for record in history:
                _validate_marker(record)
            records = history[:]
    current = {"previous_checked_present": checked_present, "previous_checked": checked}
    records.append(current)
    asset["ocr_numeric_rollback"] = current
    asset["ocr_numeric_rollback_history"] = records
    asset["numeric_fields_checked"] = False


def _preflight(
    rows: list[tuple[str, str]], originals_to: Path | None
) -> tuple[list[tuple[str, str]], dict[str, tuple[str, str, int, str]], dict[str, int]]:
    counts = {
        "workspaces": 0,
        "documents_moved": 0,
        "image_reviews_reset": 0,
        "originals_copied": 0,
        "previews_copied": 0,
    }
    updated_rows = []
    files: dict[str, tuple[str, str, int, str]] = {}
    for row_id, encoded in rows:
        data = json.loads(encoded)
        if not isinstance(data, dict) or not isinstance(data.get("assets", []), list):
            raise ValueError("invalid asset list")
        changed = False
        for asset in data.get("assets", []):
            if not isinstance(asset, dict) or not isinstance(asset.get("document"), dict):
                raise ValueError("invalid source document")
            document = asset["document"]
            present = set(NUMERIC_FIELDS).intersection(document)
            if present:
                if present != set(NUMERIC_FIELDS):
                    raise ValueError("partial numeric metadata")
                numeric = {key: document.pop(key) for key in NUMERIC_FIELDS}
                if "ocr_numeric" in asset and asset["ocr_numeric"] != numeric:
                    raise ValueError("conflicting numeric metadata")
                asset["ocr_numeric"] = numeric
                counts["documents_moved"] += 1
                changed = True
            existing_numeric = asset.get("ocr_numeric")
            if existing_numeric is not None and (
                not isinstance(existing_numeric, dict)
                or set(existing_numeric) != set(NUMERIC_FIELDS)
            ):
                raise ValueError("invalid numeric metadata")
            if document.get("kind") == "image":
                _reset_image_review(asset)
                counts["image_reviews_reset"] += 1
                changed = True
            if originals_to is not None:
                preview_fields = (
                    "preview_path",
                    "preview_storage_id",
                    "preview_size",
                    "preview_sha256",
                )
                if any(key in asset for key in preview_fields) and not all(
                    key in asset for key in preview_fields
                ):
                    raise ValueError("incomplete preview metadata")
                for prefix in ("", "preview_"):
                    if prefix and "preview_path" not in asset:
                        continue
                    spec = _source_spec(asset, prefix)
                    _read_source(spec)  # Validate every file before creating output.
                    source_id = spec[1]
                    prior = files.get(source_id)
                    if prior is not None and (prior[2], prior[3]) != (spec[2], spec[3]):
                        raise ValueError("conflicting source storage identifiers")
                    if prior is None:
                        files[source_id] = spec
                        counts["previews_copied" if prefix else "originals_copied"] += 1
                    asset[prefix + "path"] = str(originals_to / source_id)
                    changed = True
        if changed:
            updated_rows.append((json.dumps(data, ensure_ascii=False), row_id))
            counts["workspaces"] += 1
    return updated_rows, files, counts


def _write_stage(path: Path, content: bytes) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _publish(stage: Path, target: Path, published: list[tuple[Path, int, int]]) -> None:
    entry = stage.stat()
    os.link(stage, target, follow_symlinks=False)
    published.append((target, entry.st_dev, entry.st_ino))


def _remove_published(published: list[tuple[Path, int, int]]) -> None:
    failures = []
    for path, device, inode in reversed(published):
        try:
            entry = path.lstat()
            if (entry.st_dev, entry.st_ino) != (device, inode):
                failures.append(str(path))
            else:
                path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            failures.append(str(path))
    if failures:
        raise RuntimeError("cleanup incomplete; inspect newly created output before reuse")


def prepare(source: Path, destination: Path, originals_to: Path | None = None) -> dict[str, int]:
    if source.is_symlink() or not source.is_file():
        raise ValueError("source must be an existing regular SQLite file, not a symlink")
    parent = _private_directory(destination.parent)
    copied_originals = _private_directory(originals_to) if originals_to is not None else None
    if copied_originals is not None and any(copied_originals.iterdir()):
        raise ValueError("originals destination directory must be empty")
    target = parent / destination.name
    if source.resolve() == target.resolve():
        raise ValueError("source and destination must differ")
    if copied_originals == parent:
        raise ValueError("database output and originals directory must be separate")
    if target.exists() or target.is_symlink():
        raise FileExistsError("database destination already exists")

    published: list[tuple[Path, int, int]] = []
    try:
        with ExitStack() as stack:
            source_stage = Path(
                stack.enter_context(TemporaryDirectory(prefix=".oos-source-", dir=parent))
            )
            snapshot = _snapshot_source(source.absolute(), source_stage)
            source_db = stack.enter_context(closing(sqlite3.connect(snapshot)))
            source_db.execute("BEGIN")
            if source_db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ValueError("source database failed integrity check")
            rows = source_db.execute("SELECT id, data FROM review_workspace").fetchall()
            updates, files, counts = _preflight(rows, copied_originals)
            if copied_originals is not None and any(copied_originals.iterdir()):
                raise ValueError("originals destination directory is no longer empty")
            if target.exists() or target.is_symlink():
                raise FileExistsError("database destination already exists")
            db_stage_dir = Path(
                stack.enter_context(TemporaryDirectory(prefix=".oos-db-", dir=parent))
            )
            staged_db = db_stage_dir / "rollback.sqlite"
            fd = os.open(staged_db, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            with closing(sqlite3.connect(staged_db)) as copy_db:
                source_db.backup(copy_db)
                copy_db.execute("BEGIN IMMEDIATE")
                copy_db.executemany("UPDATE review_workspace SET data = ? WHERE id = ?", updates)
                copy_db.commit()
                if copy_db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise ValueError("staged database failed integrity check")
            original_stage: Path | None = None
            if copied_originals is not None:
                original_stage = Path(
                    stack.enter_context(
                        TemporaryDirectory(prefix=".oos-originals-", dir=copied_originals)
                    )
                )
                for source_id, spec in files.items():
                    # Recheck after preflight, in case an original changed meanwhile.
                    _write_stage(original_stage / source_id, _read_source(spec))
            if copied_originals is not None and original_stage is not None:
                if any(path != original_stage for path in copied_originals.iterdir()):
                    raise ValueError("originals destination directory is no longer empty")
                for source_id in files:
                    _publish(original_stage / source_id, copied_originals / source_id, published)
            # Publish the database last, only after every original was staged.
            _publish(staged_db, target, published)
    except BaseException:
        _remove_published(published)
        raise
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument(
        "--copy-originals-to",
        type=Path,
        help="existing empty private directory for verified original and preview copies",
    )
    args = parser.parse_args()
    try:
        counts = prepare(args.source, args.destination, args.copy_originals_to)
    except (OSError, sqlite3.Error, ValueError, TypeError, RuntimeError) as exc:
        parser.exit(1, f"Conversion failed: {exc}\n")
    print(json.dumps(counts, ensure_ascii=False))


if __name__ == "__main__":
    main()
