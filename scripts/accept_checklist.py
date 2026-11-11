"""Real local HTTP checklist journey. Manual by default; live is explicitly gated.

This is one synthetic development smoke, not a held-out evaluation or browser test.
"""

import argparse
import csv
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
from decimal import Decimal
from io import BytesIO, StringIO
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

import httpx
import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adapters.llm import validated_live_provider  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db.schema import init_db  # noqa: E402
from app.db.session import make_engine  # noqa: E402
from app.review.sample import SAMPLE_SNAPSHOT  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def save(output: Path, name: str, value: object) -> None:
    (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def business_counts(database: Path) -> dict[str, int]:
    with sqlite3.connect(database) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT IN "
                "('review_workspace','model_usage','call_trace','alembic_version')"
            )
        ]
        # Table identifiers originate in trusted migrated schema, never input text.
        return {
            name: connection.execute(
                'SELECT count(*) FROM "' + name.replace('"', '""') + '"'
            ).fetchone()[0]
            for name in tables
        }


def run(output: Path, *, live: bool, ledger: Path | None, acknowledge: bool) -> None:
    os.umask(0o077)
    output = output.resolve()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    if live:
        if not acknowledge or ledger is None or not ledger.is_absolute():
            raise ValueError("真实冒烟须显式确认合成传输并指定同轮共用的绝对费用库路径")
        # Credentials are read only by normal configuration/transport. Never dump settings.
        settings = Settings(_env_file=None)
        validated_live_provider(settings)
        if settings.llm_provider != "openai" or settings.llm_model != "gpt-6-luna":
            raise ValueError("本轮先固定OpenAI gpt-6-luna，不静默换模型")
        if str(settings.price_in_per_mtok) != "0.125" or str(settings.price_out_per_mtok) not in {
            "0.50",
            "0.5",
        }:
            raise ValueError("冒烟按输入0.125/输出0.50每百万token保守预留，不猜单价")
        if (
            not 0 < settings.live_budget_usd <= Decimal("0.20")
            or not 1 <= settings.model_max_tokens <= 3000
        ):
            raise ValueError("首批冒烟费用上限须大于0且不超过0.20美元，输出不超过3000token")
        if not ledger.parent.is_dir() or ledger.parent.stat().st_mode & 0o077:
            raise ValueError("共用费用库父目录须预先创建为私有目录")
        database = ledger
    else:
        database = output / "synthetic.db"
    engine = make_engine(f"sqlite:///{database}")
    try:
        init_db(engine)
    finally:
        engine.dispose()
    before = business_counts(database)
    if any(before.values()):
        raise ValueError("只使用本轮专用空业务库；拒绝在含现有业务的库上运行")
    save(output, "business-before.json", before)
    env = {key: value for key, value in os.environ.items() if not key.startswith("OOS_")}
    if live:
        env.update({key: value for key, value in os.environ.items() if key.startswith("OOS_")})
    env.update(
        OOS_DATABASE_URL=f"sqlite:///{database}", OOS_LIVE_ENABLED="true" if live else "false"
    )
    # Retain proxy/CA settings and the normal inherited secret binding, never print them.
    address = socket.socket()
    address.bind(("127.0.0.1", 0))
    address.listen()
    port = address.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    with (output / "server.log").open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--fd", str(address.fileno())],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            pass_fds=(address.fileno(),),
        )
    address.close()
    try:
        with httpx.Client(base_url=base, trust_env=False, timeout=90) as client:
            deadline = monotonic() + 20
            while True:
                try:
                    if client.get("/health").is_success:
                        break
                except httpx.TransportError:
                    pass
                if process.poll() is not None or monotonic() > deadline:
                    raise RuntimeError("本地服务未启动；请查看脱敏server.log")
                sleep(0.05)
            snapshot = json.loads(json.dumps(SAMPLE_SNAPSHOT))
            order_id = "SYN-" + uuid4().hex[:8]
            snapshot["orders"][0]["order_id"] = order_id
            text = f"麻烦核对 {order_id}：00123 藏蓝 M，最终订货总量要37件；已经发货的保持。谢谢。"
            raw = {
                "request_key": uuid4().hex,
                "selected_order_id": order_id,
                "instruction": text,
                "snapshot": snapshot,
            }
            save(output, "input.json", raw)
            created = client.post("/reviews", json=raw)
            created.raise_for_status()
            item = created.json()
            review_id = item["id"]
            save(output, "created.json", item)
            assert client.post("/reviews", json=raw).json()["id"] == review_id
            if live:
                extracted = client.post(
                    f"/reviews/{review_id}/extract",
                    json={
                        "revision": item["revision"],
                        "request_key": uuid4().hex,
                        "acknowledge_transmission": True,
                    },
                )
                save(output, "extraction-response.json", extracted.json())
                extracted.raise_for_status()
                item = extracted.json()
                assert not item["data"]["human"]
            else:
                correction = {
                    "order_id": order_id,
                    "disposition": "propose",
                    "questions": [],
                    "summary": "人工输入合成提案；不代表真实模型理解",
                    "changes": [
                        {
                            "mode": "set_total",
                            "sku_id": "00123-藏蓝-M",
                            "to_sku": None,
                            "quantity": 37,
                            "unit": "件",
                            "before_qty": 50,
                            "after_qty": 37,
                            "citations": [],
                        }
                    ],
                }
                updated = client.post(
                    f"/reviews/{review_id}/correct",
                    json={
                        "revision": 1,
                        "proposal": correction,
                        "reason": "确定性本地人工流程验收",
                    },
                )
                updated.raise_for_status()
                item = updated.json()
            save(output, "proposal-and-rules.json", item)
            assert item["data"]["check"]["status"] == "ready"
            row = item["data"]["check"]["rows"][0]
            assert (row["old_qty"], row["new_qty"], row["delta"]) == (50, 37, -13)
            approved = client.post(
                f"/reviews/{review_id}/confirm", json={"revision": item["revision"]}
            )
            approved.raise_for_status()
            save(output, "confirmed.json", approved.json())
            exports = {}
            for fmt in ("csv", "xlsx"):
                response = client.get(f"/reviews/{review_id}/export", params={"fmt": fmt})
                response.raise_for_status()
                data = response.content
                (output / f"checklist.{fmt}").write_bytes(data)
                if fmt == "csv":
                    rows = list(csv.DictReader(StringIO(data.decode("utf-8-sig"))))
                else:
                    book = openpyxl.load_workbook(BytesIO(data), data_only=False)
                    values = list(book.active.values)
                    rows = [dict(zip(values[0], line, strict=True)) for line in values[1:]]
                    book.close()
                assert len(rows) == 1 and int(rows[0]["new_qty"]) == 37
                assert "未执行订单" in rows[0]["state"]
                exports[fmt] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            save(output, "export-checks.json", exports)
            after = business_counts(database)
            save(output, "business-after.json", after)
            assert after == before
            sha, dirty = None, None
            try:
                sha = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
                dirty = bool(
                    subprocess.run(
                        ["git", "status", "--porcelain"],
                        cwd=ROOT,
                        capture_output=True,
                        text=True,
                        check=True,
                    ).stdout
                )
            except (OSError, subprocess.CalledProcessError):
                # A source archive has no Git metadata. Keep measured file
                # hashes and never invent a verified commit or clean state.
                pass
            files = [
                p
                for folder in ("app", "schemas", "order_ops_migrations")
                for p in (ROOT / folder).rglob("*.py")
            ]
            files += [Path(__file__), ROOT / "pyproject.toml", ROOT / "requirements.lock"]
            save(
                output,
                "SOURCE.json",
                {
                    str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted(files)
                },
            )
            save(
                output,
                "RESULT.json",
                {
                    "passed": True,
                    "mode": "REAL_MODEL" if live else "MANUAL",
                    "synthetic": True,
                    "code_sha": sha,
                    "working_tree_dirty": dirty,
                    "source_provenance": "git_and_file_hashes" if sha else "file_hashes_only",
                    "browser_test": False,
                    "review_id": review_id,
                    "business_unchanged": True,
                    "heldout_evaluation": False,
                },
            )
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--acknowledge-synthetic-transmission", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("--output 必须是不存在的新目录；不会覆盖已有验收结果")
    try:
        run(
            args.output,
            live=args.live,
            ledger=args.ledger,
            acknowledge=args.acknowledge_synthetic_transmission,
        )
    except Exception:
        # Never print exception details that could contain provider or DB data.
        if args.output.is_dir():
            save(
                args.output,
                "RESULT.json",
                {
                    "passed": False,
                    "mode": "LIVE" if args.live else "MANUAL",
                    "message": "验收未通过；原输入与已完成步骤保留，请查看结构化结果及服务日志",
                },
            )
        print("Checklist acceptance FAILED; inspect retained evidence.", file=sys.stderr)
        raise SystemExit(1) from None
    print("Checklist acceptance passed; this is not a browser or held-out evaluation.")
