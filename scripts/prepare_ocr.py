"""Install pinned public Tesseract language data into a user-owned directory.

No customer data is sent. No language weights are included in the application package.
"""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.source_storage import new_original, prepare_source_storage  # noqa: E402

COMMIT = "87416418657359cb625c412a48b6e1d6d41c29bd"
FILES = {
    "chi_sim.traineddata": "a5fcb6f0db1e1d6d8522f39db4e848f05984669172e584e8d76b6b3141e1f730",
    "eng.traineddata": "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2",
    "LICENSE": "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
}
BEST_COMMIT = "e12c65a915945e4c28e237a9b52bc4a8f39a0cec"
BEST_FILES = {
    "chi_sim.traineddata": "4fef2d1306c8e87616d4d3e4c6c67faf5d44be3342290cf8f2f0f6e3aa7e735b",
    "chi_sim_vert.traineddata": "ea672a78157199c333aa12ec4e74550077689b545df5fc770903716850c8b2e5",
    "eng.traineddata": "8280aed0782fe27257a68ea10fe7ef324ca0f8d85bd2fd145d1c2b560bcb66ba",
    "LICENSE": "a6cba85bc92e0cff7a450b1d873c0eaa2e9fc96bf472df0247a26bec77bf3ff9",
}
PROFILES = {
    "fast": ("tessdata_fast", COMMIT, FILES),
    "best": ("tessdata_best", BEST_COMMIT, BEST_FILES),
}
MAX_DATA_BYTES = 20_000_000


def prepare(directory: Path, profile: str = "fast") -> dict:
    os.umask(0o077)
    if not directory.is_absolute():
        raise ValueError("OCR目录须为绝对路径")
    if profile not in PROFILES:
        raise ValueError("不支持的OCR数据配置")
    repository, commit, files = PROFILES[profile]
    prepare_source_storage(directory)
    manifest = {
        "repository": f"tesseract-ocr/{repository}",
        "commit": commit,
        "profile": profile,
        "files": {},
    }
    # Check all existing files before downloading anything, so an attempted
    # switch of profile cannot partially populate an incompatible directory.
    for name, expected in files.items():
        path = directory / name
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_DATA_BYTES:
                raise ValueError("已有OCR文件不可用，不覆盖")
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("已有OCR文件与固定哈希不符，请人工检查，不覆盖")
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        for name, expected in files.items():
            path = directory / name
            url = f"https://raw.githubusercontent.com/tesseract-ocr/{repository}/{commit}/{name}"
            if path.exists() or path.is_symlink():
                if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_DATA_BYTES:
                    raise ValueError("已有OCR文件不可用，不覆盖")
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() != expected:
                    raise ValueError("已有OCR文件与固定哈希不符，请人工检查，不覆盖")
            else:
                with client.stream("GET", url) as response:
                    response.raise_for_status()
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        if len(body) + len(chunk) > MAX_DATA_BYTES:
                            raise ValueError("OCR下载超过上限")
                        body.extend(chunk)
                data = bytes(body)
                if hashlib.sha256(data).hexdigest() != expected:
                    raise ValueError("OCR下载哈希不符，未保存")
                with new_original(directory, name) as (_, stream):
                    stream.write(data)
            manifest["files"][name] = {"sha256": expected, "bytes": len(data), "url": url}
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--profile", choices=tuple(PROFILES), default="fast")
    args = parser.parse_args()
    try:
        manifest = prepare(args.directory, args.profile)
    except Exception:
        print(
            "OCR setup failed; verify directory permissions, network and pinned hashes.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(json.dumps(manifest, indent=2))
