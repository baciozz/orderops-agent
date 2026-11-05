"""Check an already-built wheel's resources against this source tree, offline.

Usage: python scripts/check_distribution.py path/to/package.whl
This does not install dependencies or replace installed-runtime migration testing.
"""

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check(wheel: Path) -> dict[str, str]:
    required = [ROOT / "order_ops_migrations" / "__init__.py"]
    required += sorted((ROOT / "order_ops_migrations").rglob("*.py"))
    required += [ROOT / "order_ops_migrations" / "script.py.mako"]
    required += sorted((ROOT / "app/web/templates").glob("*.html"))
    required += sorted(
        p for p in (ROOT / "app/web/static").glob("*") if p.is_file() and not p.name.startswith(".")
    )
    hashes = {}
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or any(n.startswith("alembic/") for n in names):
            raise ValueError("Duplicate entries or conflicting alembic package in wheel")
        for source in sorted(set(required)):
            name = source.relative_to(ROOT).as_posix()
            contents = archive.read(name)
            if contents != source.read_bytes():
                raise ValueError(f"Wheel resource differs from source: {name}")
            hashes[name] = hashlib.sha256(contents).hexdigest()
        expected = {p.relative_to(ROOT).as_posix() for p in required}
        migrations = {n for n in names if n.startswith("order_ops_migrations/")}
        if migrations != {n for n in expected if n.startswith("order_ops_migrations/")}:
            raise ValueError("Unexpected or missing migration package entries")
    return hashes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    print(json.dumps(check(args.wheel), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
