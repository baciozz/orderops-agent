"""Stopped local review workspace backup/restore. No online consistency promise."""

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
from pathlib import Path


def private_absolute(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise ValueError("Use a private absolute path, without '..'")
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError("Symlink paths are not supported")
    return path


def digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Only regular backup files are supported")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def refs(database: Path, root: Path) -> dict[str, dict]:
    result: dict[str, dict] = {}
    with sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("Database integrity check failed")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
        if (
            "input_source" in tables
            and db.execute("SELECT count(*) FROM input_source").fetchone()[0]
        ):
            raise ValueError("Legacy input_source files need the separate legacy backup procedure")
        for (raw,) in db.execute("SELECT data FROM review_workspace"):
            for asset in json.loads(raw).get("assets", []):
                for prefix in ("", "preview_"):
                    if prefix + "path" not in asset:
                        continue
                    name = asset[prefix + "storage_id"]
                    if not re.fullmatch(r"RVO-[0-9a-f]{32}", name):
                        raise ValueError("Unsupported source identity")
                    if Path(asset[prefix + "path"]) != root / name:
                        raise ValueError(
                            "All referenced review files must use the supplied original root"
                        )
                    value = {"size": asset[prefix + "size"], "sha256": asset[prefix + "sha256"]}
                    if name in result and result[name] != value:
                        raise ValueError("Conflicting source metadata")
                    result[name] = value
    return result


def inventory(root: Path) -> dict[str, dict]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Original root must be a real directory")
    result = {}
    for path in sorted(root.iterdir()):
        if (
            path.is_symlink()
            or not path.is_file()
            or not re.fullmatch(r"RVO-[0-9a-f]{32}", path.name)
        ):
            raise ValueError("Original root must contain only regular RVO files")
        result[path.name] = {"size": path.stat().st_size, "sha256": digest(path)}
    return result


def backup(database: Path, root: Path, archive: Path) -> None:
    if archive.exists() or not database.is_file() or not root.is_dir():
        raise ValueError("Use an existing stopped database/source root and a NEW archive directory")
    archive.mkdir(mode=0o700)
    # Source is deliberately NOT immutable: committed WAL must not be ignored.
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as source:
        with sqlite3.connect(archive / "review.db") as target:
            source.backup(target)
    expected = refs(archive / "review.db", root)
    files = inventory(root)
    if any(files.get(name) != value for name, value in expected.items()):
        raise ValueError(
            "A referenced original/preview is missing or changed; archive is incomplete"
        )
    shutil.copytree(root, archive / "originals")
    if inventory(archive / "originals") != files:
        raise ValueError("Copied originals differ; archive is incomplete")
    manifest = {
        "schema": 1,
        "kind": "stopped local review backup; trusted local manifest, not a signature",
        "original_root": str(root),
        "database_sha256": digest(archive / "review.db"),
        "referenced_files": len(expected),
        "files": files,
    }
    (archive / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    for path in archive.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)
    print(json.dumps({"backup": "verified", "files": len(files), "referenced": len(expected)}))


def restore(archive: Path, root: Path, database: Path) -> None:
    manifest = json.loads((archive / "manifest.json").read_text())
    if (
        root.exists()
        or database.exists()
        or not database.parent.is_dir()
        or not root.parent.is_dir()
    ):
        raise ValueError(
            "Restore requires NEW database/root paths, with existing private parent directories"
        )
    if manifest["schema"] != 1 or manifest["original_root"] != str(root):
        raise ValueError(
            "Originals must return to their recorded absolute root; relocation is unsupported"
        )
    if digest(archive / "review.db") != manifest["database_sha256"]:
        raise ValueError("Backup database hash differs")
    files = inventory(archive / "originals")
    expected = refs(archive / "review.db", root)
    if files != manifest["files"] or any(files.get(k) != v for k, v in expected.items()):
        raise ValueError("Backup originals are incomplete or changed")
    shutil.copytree(archive / "originals", root)
    shutil.copyfile(archive / "review.db", database)
    root.chmod(0o700)
    for path in root.iterdir():
        path.chmod(0o600)
    database.chmod(0o600)
    print(json.dumps({"restore": "verified", "files": len(files), "referenced": len(expected)}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("backup", "restore"))
    parser.add_argument("--database", required=True, type=private_absolute)
    parser.add_argument("--originals", required=True, type=private_absolute)
    parser.add_argument("--archive", required=True, type=private_absolute)
    parser.add_argument("--all-writers-stopped", action="store_true")
    args = parser.parse_args()
    if not args.all_writers_stopped:
        parser.error(
            "Stop ALL writers first; the flag acknowledges this prerequisite, it cannot detect them"
        )
    os.umask(0o077)
    try:
        if args.action == "backup":
            backup(args.database, args.originals, args.archive)
        else:
            restore(args.archive, args.originals, args.database)
    except (OSError, ValueError, sqlite3.Error, KeyError, TypeError):
        parser.exit(
            1,
            "Backup/restore refused or incomplete; keep the source "
            "and inspect trusted local files.\n",
        )


if __name__ == "__main__":
    main()
