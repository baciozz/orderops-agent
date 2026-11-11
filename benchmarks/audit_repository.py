"""Read-only release-scope inventory. Findings contain locations, never matched values."""

import argparse
import json
import re
import subprocess
from collections import Counter
from pathlib import Path

PATTERNS = {
    "credential-shaped": re.compile(
        rb"(?<![A-Za-z0-9])(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|"
        rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"
    ),
    "email-shaped": re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "private-path": re.compile(
        rb"(?:/workspace/|/home/(?!runner/)[A-Za-z0-9_-]+/|[A-Z]:\\\\Users\\\\)"
    ),
}


def git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo)


def audit(repo):
    objects = git(repo, "rev-list", "--objects", "--all").decode().splitlines()
    names = {
        line.split(" ", 1)[0]: line.split(" ", 1)[1] if " " in line else "" for line in objects
    }
    meta = (
        git(
            repo,
            "cat-file",
            "--batch-all-objects",
            "--batch-check=%(objectname) %(objecttype) %(objectsize)",
        )
        .decode()
        .splitlines()
    )
    # Only reachable objects belong to this claim, not dangling/unfetched data.
    blobs = [
        (sha, int(size))
        for sha, kind, size in map(str.split, meta)
        if kind == "blob" and sha in names
    ]
    findings = []
    binary = []
    oversized = []
    text_count = 0
    for sha, size in blobs:
        path = names[sha]
        if size > 10_000_000:
            oversized.append(dict(blob=sha, path=path, size=size))
            continue
        data = git(repo, "cat-file", "blob", sha)
        if b"\0" in data:
            binary.append(dict(blob=sha, path=path, size=size))
            continue
        text_count += 1
        for kind, pattern in PATTERNS.items():
            locations = [i for i, line in enumerate(data.splitlines(), 1) if pattern.search(line)]
            if locations:
                findings.append(dict(kind=kind, blob=sha, path=path, lines=locations))
    refs = (
        git(
            repo,
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            "refs/remotes/origin",
            "refs/remotes/audit-pr",
            "refs/tags",
        )
        .decode()
        .splitlines()
    )
    emails = set(git(repo, "log", "--all", "--format=%ae%n%ce").decode().splitlines())
    personal = [
        email
        for email in emails
        if email and not any(x in email for x in ("noreply.github.com", "example.", "localhost"))
    ]
    return dict(
        schema_version=1,
        kind="read-only heuristic publication inventory; not security clearance",
        head=git(repo, "rev-parse", "HEAD").decode().strip(),
        shallow=git(repo, "rev-parse", "--is-shallow-repository").decode().strip(),
        commits=int(git(repo, "rev-list", "--all", "--count")),
        refs=refs,
        ref_count=len(refs),
        reachable_objects=len(objects),
        blobs=len(blobs),
        text_blobs_scanned=text_count,
        binary_blobs_not_content_scanned=binary,
        oversized_unscanned=oversized,
        findings=findings,
        counts=dict(Counter(row["kind"] for row in findings)),
        distinct_commit_emails=len(emails),
        personal_commit_email_count=len(personal),
        publication_recommendation=(
            "Keep development repository private; prepare curated "
            "no-history snapshot for separate review"
        ),
        not_covered=[
            "unreachable/deleted objects not advertised by GitHub",
            "all binary embedded content",
            "GitHub discussions/logs/assets audited separately",
            "ownership/license decisions",
        ],
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output must be new")
    result = audit(args.repo)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "head",
                    "commits",
                    "ref_count",
                    "reachable_objects",
                    "blobs",
                    "text_blobs_scanned",
                    "counts",
                    "personal_commit_email_count",
                )
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
