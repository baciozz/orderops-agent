"""Probe only the installed wheel, with explicit synthetic SQLite and no settings."""

import hashlib
import importlib.abc
import importlib.metadata
import json
import os
import sys
import sysconfig
import zipfile
from pathlib import Path


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


class BlockApplication(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "app.main":
            raise RuntimeError(f"Forbidden application import: {fullname}")
        return None


def main() -> None:
    wheel, database = (Path(arg).resolve() for arg in sys.argv[1:])
    require(not database.exists(), "Probe requires a fresh synthetic database")
    site = Path(sysconfig.get_path("purelib")).resolve()
    require(site.is_relative_to(Path(sys.prefix).resolve()), "Runtime is not isolated")
    dist = importlib.metadata.distribution("order-ops-superagent")
    direct = json.loads(dist.read_text("direct_url.json") or "{}")
    require(not direct.get("dir_info", {}).get("editable"), "Editable install forbidden")
    expected_hash = hashlib.sha256(wheel.read_bytes()).hexdigest()
    require(
        direct.get("archive_info", {}).get("hashes", {}).get("sha256") == expected_hash,
        "Installed distribution is not the selected wheel",
    )
    with zipfile.ZipFile(wheel) as archive:
        expected = {
            name: hashlib.sha256(archive.read(name)).hexdigest()
            for name in archive.namelist()
            if name.startswith(("app/", "schemas/", "seeds/", "order_ops_migrations/"))
        }

    def hashes():
        return {name: hashlib.sha256((site / name).read_bytes()).hexdigest() for name in expected}

    require(hashes() == expected, "Installed bytes differ from wheel")
    sys.meta_path.insert(0, BlockApplication())

    def guard(event, args):
        if event == "sqlite3.connect" and Path(args[0]).resolve() != database:
            raise RuntimeError("Only explicit synthetic database allowed")
        if event.startswith("socket."):
            raise RuntimeError("Network forbidden during installed probe")
        if event == "open":
            path, mode, flags = args
            if isinstance(path, (str, bytes, os.PathLike)):
                resolved = Path(os.fsdecode(path)).resolve()
                if resolved.name == ".env":
                    raise RuntimeError("Environment-file reads forbidden")
                writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                    isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)
                )
                if writing and resolved.is_relative_to(site):
                    raise RuntimeError("Installed resource writes forbidden")
        if event in {"os.remove", "os.rename", "os.rmdir", "os.mkdir"}:
            for path in args[:2] if event == "os.rename" else args[:1]:
                if isinstance(path, (str, bytes, os.PathLike)):
                    if Path(os.fsdecode(path)).resolve().is_relative_to(site):
                        raise RuntimeError("Installed resource mutations forbidden")

    sys.addaudithook(guard)
    from pydantic_settings import BaseSettings

    def no_settings(*args, **kwargs):
        raise RuntimeError("Settings construction forbidden")

    BaseSettings.__init__ = no_settings
    from alembic.script import ScriptDirectory
    from app.db.schema import _migration_config, init_db, schema_ready
    from app.db.session import make_engine
    from sqlalchemy import text

    scripts = ScriptDirectory.from_config(_migration_config())
    heads = scripts.get_heads()
    require(len(heads) == 1, "Expected exactly one packaged migration head")
    engine = make_engine("sqlite:///" + str(database))
    try:
        init_db(engine)
        require(schema_ready(engine), "Fresh schema not ready")

        def snapshot():
            with engine.connect() as connection:
                require(
                    connection.execute(text("SELECT version_num FROM alembic_version")).all()
                    == [(heads[0],)],
                    "Wrong version rows",
                )
                return connection.execute(
                    text("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")
                ).all()

        before = snapshot()
        init_db(engine)
        require(snapshot() == before and schema_ready(engine), "Repeated init changed schema")
    finally:
        engine.dispose()
    origins = {}
    for name, module in list(sys.modules.items()):
        if name.split(".")[0] in {"app", "schemas", "seeds", "order_ops_migrations"}:
            origin = Path(module.__file__).resolve()
            require(origin.is_relative_to(site), f"Source import: {name}")
            require(origin.relative_to(site).as_posix() in expected, f"Unexpected module: {name}")
            origins[name] = str(origin)
    require(hashes() == expected, "Installed resource bytes changed")
    print(
        json.dumps(
            {
                "wheel_sha256": expected_hash,
                "head": heads[0],
                "origins": origins,
                "verified_installed_files": len(expected),
                "repeat_init": "passed",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
