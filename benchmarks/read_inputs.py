"""Read only declared public assets. Never open reference labels or rendering text."""

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from time import perf_counter

from app.errors import AppError
from app.review.contracts import Snapshot
from app.review.documents import read_document


def quantiles(values):
    values = sorted(values)
    return (
        None
        if not values
        else dict(
            n=len(values),
            p50=median(values),
            p95=values[max(0, (len(values) * 95 + 99) // 100 - 1)],
        )
    )


def run(root, output, ocr):
    output.mkdir(parents=False, exist_ok=False)
    cases = json.loads((root / "cases.json").read_text()) + json.loads(
        (root / "risks.json").read_text()
    )
    results = []
    for case in cases:
        start = perf_counter()
        result = {key: case[key] for key in ("case_id", "story_id", "split", "form", "category")}
        try:
            source = root / case["input_asset"]
            snapshot = Snapshot.model_validate_json((root / case["baseline_asset"]).read_bytes())
            assert snapshot.synthetic
            doc, _ = read_document(
                source.name,
                source.read_bytes(),
                encoding=case["encoding"],
                rotation=case["rotation"],
                ocr_data_dir=ocr,
            )
            result.update(
                status="READ",
                confirmable=doc.confirmable,
                spans=len(doc.spans),
                warnings=doc.warnings,
                ocr_version=doc.ocr_version,
            )
            # No document text in benchmark index; raw pixels/originals remain in input bundle.
        except AppError as exc:
            result.update(status="ERROR", error_code=exc.code)
        except Exception:
            result.update(status="ERROR", error_code="READER_RUN_FAILED")
        result["parse_or_ocr_ms"] = round((perf_counter() - start) * 1000, 3)
        results.append(result)
        with (output / "read-results.jsonl").open("a") as stream:
            stream.write(json.dumps(result, ensure_ascii=False) + "\n")
    timings = defaultdict(list)
    for result in results:
        timings[result["form"]].append(result["parse_or_ocr_ms"])
    report = dict(
        kind="actual byte reading only; not extraction accuracy or model evaluation",
        model_calls=0,
        total=len(results),
        outcomes=dict(Counter(row["status"] for row in results)),
        failure_cases=[row for row in results if row["status"] != "READ"],
        latency_ms={form: quantiles(values) for form, values in timings.items()},
        model_ms=None,
        rules_ms=None,
        human_wait_ms=None,
        fee_usd=0,
        freeze_sha256=hashlib.sha256((root / "FREEZE.json").read_bytes()).hexdigest(),
    )
    (output / "REPORT.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ocr-data-dir", type=Path)
    args = parser.parse_args()
    run(args.dataset, args.output, args.ocr_data_dir)


if __name__ == "__main__":
    main()
