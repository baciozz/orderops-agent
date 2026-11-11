"""Build a private no-history source review bundle. Never publish or approve it."""

import argparse
import gzip
import hashlib
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

ROOT_FILES = (
    "LICENSE",
    "pyproject.toml",
    "MANIFEST.in",
    "Makefile",
    "requirements.lock",
    "requirements-dev.lock",
    "requirements-build.lock",
    ".env.example",
    "alembic.ini",
    "ARCHITECTURE.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    ".github/workflows/ci.yml",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
)
DIRECTORIES = (
    "app",
    "schemas",
    "seeds",
    "order_ops_migrations",
    "tests",
    "scripts",
    "benchmarks",
    "evals",
)
DOCS = (
    "LOCAL_BETA_GUIDE.zh-CN.md",
    "LOCAL_BETA_ARCHITECTURE.zh-CN.md",
    "LOCAL_BETA_LIMITATIONS.zh-CN.md",
    "REVIEW_OFFLINE_BACKUP.zh-CN.md",
    "OFFLINE_BACKUP_RESTORE.zh-CN.md",
    "THIRD_PARTY_NOTICES.md",
    "ARCHITECTURE.md",
    "API.md",
    "SUBAGENTS.md",
    "KNOWN_LIMITATIONS.md",
    "LIVE_EVAL.md",
    "TEST_CHECKLIST.md",
    "WORKER_LOOP.md",
    "MODEL_PROVIDERS.md",
    "browser-security.md",
    "OBSERVABILITY.md",
    "OFFLINE_EVAL.md",
    "beta/INPUT_SUPPORT.zh-CN.md",
    "beta/DATASET.zh-CN.md",
    "beta/HELDOUT_INDEPENDENCE.zh-CN.md",
    "beta/AGENT_COMPARISON.zh-CN.md",
    "beta/SINGLE_MODEL_BASELINE.zh-CN.md",
    "beta/EVALUATION_PROTOCOL.zh-CN.md",
    "beta/REAL_MODEL_RECORDING.zh-CN.md",
    "beta/INDEPENDENT_TRIAL.zh-CN.md",
    "beta/INDEPENDENT_TRIAL_RECORD.json",
    "beta/evidence/DEPENDENCY_LICENSE_DECLARATIONS.json",
)
README = """# OrderOps private source review candidate

This is a private, no-history review snapshot, not an approved release or permission to publish.
The existing package version 1.0.0rc1 is historical. No new tag or Release is created.

The local checklist workspace imports order/inventory/fulfillment snapshots and customer requests,
checks model proposals with deterministic rules, permits full human correction and exports CSV/XLSX.
It cannot execute orders, reserve inventory or submit to an ERP. Legacy FIXTURE business demos are
separate. Use synthetic data and 127.0.0.1 only.
Authentication and shared deployment are unsupported.

## Source installation

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock -r requirements.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
mkdir -m 700 local-preview
export OOS_DATABASE_URL="sqlite:///$PWD/local-preview/review.db"
export OOS_LIVE_ENABLED=false
.venv/bin/python -m app.cli init-db
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000/ui/reviews . With no model key, explicitly marked manual correction can
demonstrate rules and exports; this is not a model score. Server-only model configuration and
transmission acknowledgement are described in the local guide. Never paste keys into chat or forms.

Install requirements-dev.lock to run `make check` and `make eval-offline`. CI stays offline without
paid credentials. Read [input limits](docs/beta/INPUT_SUPPORT.zh-CN.md),
[local guide](docs/LOCAL_BETA_GUIDE.zh-CN.md), [API](docs/LOCAL_BETA_ARCHITECTURE.zh-CN.md),
[limitations](docs/LOCAL_BETA_LIMITATIONS.zh-CN.md), [backup](docs/REVIEW_OFFLINE_BACKUP.zh-CN.md),
[third-party notices](docs/THIRD_PARTY_NOTICES.md), [security reporting](SECURITY.md) and
[contribution guide](CONTRIBUTING.md). Historical kernel documents retain their original context.

The included v3 synthetic reference package is development material. AI has reviewed risk labels;
it is not an independent secret held-out set. Human reference review, formal real-model/baseline
metrics, independent user trial, a qualifying recording, ownership and maintainer approval remain
pending. Development observations are not advertised accuracy or multi-Agent superiority.

The manifest fixes the source commit and per-file hashes. Git history, internal acceptance plans,
raw logs, browser video/downloads, databases, runtime configuration, credentials, OCR/font/model
weights and hidden customer data are excluded. Synthetic credential-like test strings remain in
security tests for review. A pattern scan is not security or legal clearance.
This bundle is private.
"""


def build(repo: Path, output: Path) -> None:
    if output.exists():
        raise ValueError("Output must be new")
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=repo).strip():
        raise ValueError("Commit source changes before building the review bundle")
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    tracked = set(subprocess.check_output(["git", "ls-files"], cwd=repo, text=True).splitlines())
    selected = set(ROOT_FILES) | {"docs/" + path for path in DOCS}
    selected |= {p for p in tracked if p.split("/", 1)[0] in DIRECTORIES}
    # Keep the packaging input closure too: an otherwise runnable source
    # snapshot must also support its documented ordinary-wheel check.
    for line in (repo / "MANIFEST.in").read_text().splitlines():
        words = line.split()
        if not words or words[0].startswith("#"):
            continue
        if words[0] != "include" or any("*" in word or "?" in word for word in words[1:]):
            raise ValueError("Review new packaging directives before extending the candidate scope")
        selected.update(words[1:])
    if not selected <= tracked:
        raise ValueError("An allowlisted source is not tracked")
    output.mkdir(mode=0o700)
    source = output / "source"
    source.mkdir(mode=0o700)
    for name in sorted(selected):
        path = repo / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Only regular tracked source files may enter the bundle")
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        target.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)
    (source / "README.md").write_text(README)
    (source / "CHANGELOG.md").write_text(
        (repo / "CHANGELOG.md").read_text().split("## [Unreleased]", 1)[0]
    )
    files = {
        str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(source.rglob("*"))
        if p.is_file()
    }
    manifest = {
        "kind": "private source review only, not an approved release",
        "source_sha": sha,
        "git_history_included": False,
        "public_release_approved": False,
        "documentation": "candidate-specific README and current changelog; source code unmodified",
        "files": files,
        "excluded_tracked_files": sorted(tracked - selected),
    }
    (output / "MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

    def clean_metadata(item: tarfile.TarInfo) -> tarfile.TarInfo:
        item.uid = item.gid = item.mtime = 0
        item.uname = item.gname = ""
        item.mode = 0o755 if item.isdir() or item.mode & 0o111 else 0o644
        item.pax_headers = {}
        return item

    with (output / "PRIVATE_SOURCE_REVIEW.tar.gz").open("wb") as stream:
        with gzip.GzipFile(filename="", fileobj=stream, mode="wb", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                archive.add(source, arcname="source", filter=clean_metadata)
                archive.add(
                    output / "MANIFEST.json", arcname="MANIFEST.json", filter=clean_metadata
                )
    print(json.dumps({"source_sha": sha, "files": len(files), "public_release_approved": False}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.repo.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()
