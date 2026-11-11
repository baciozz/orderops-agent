"""Zero-call integrity checks for the frozen live_v1 development dataset.

No provider, credential or network is used. The rules kernel only recomputes the
reference check from the AI-drafted oracle; it does not judge model quality.
"""

import ast
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

import pytest
from app.orchestration.fixtures import TEXT_BLACK_TO_WHITE
from app.review.contracts import CreateReview, Proposal, Snapshot
from app.review.rules import check_proposal
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "evals" / "live_v1"
MANIFEST = DATA / "manifest.json"
NOTICE = "开发期新样本，非保留集，参考由 AI 起草、待 Qi 复核"
HANDOFF_CATEGORIES = {5, 12}
MODES = {"set_total", "increment", "replace_remaining", "cancel_remaining"}
ORACLE_KEYS = {
    "allowed_proposals",
    "expected_check",
    "expected_check_variants",
    "necessary_fact_codes",
    "rationale",
    "handoff",
    "handoff_or_no_proposal",
    "category",
    "category_no",
}
SPLIT = re.compile(r"[。！？；;!?\n]+")
SHINGLE = 12


def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def load(rel: str) -> dict:
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def pairs():
    for entry in manifest()["samples"]:
        yield entry, load(entry["sample_path"]), load(entry["oracle_path"])


def norm(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text).lower()


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def protected_texts() -> list[str]:
    texts = [TEXT_BLACK_TO_WHITE]
    tree = ast.parse((ROOT / "scripts" / "live_smoke.py").read_text(encoding="utf-8"))
    samples = next(
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "SAMPLES" for t in node.targets)
    )
    texts += [
        n.value
        for n in ast.walk(samples)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    texts += list(strings(json.loads((ROOT / "evals" / "offline_cases.json").read_text())))
    for path in sorted((ROOT / "tests" / "fixtures" / "review_development").rglob("*")):
        if path.suffix == ".json":
            texts += list(strings(json.loads(path.read_text(encoding="utf-8"))))
        elif path.suffix == ".txt":
            texts.append(path.read_text(encoding="utf-8"))
        elif path.suffix == ".pdf":
            texts += [page.extract_text() or "" for page in PdfReader(path).pages]
    return texts


def our_texts() -> list[str]:
    texts = []
    for _, sample, oracle in pairs():
        texts.append(sample["request"]["instruction"])
        for proposal in oracle["allowed_proposals"]:
            texts += proposal["questions"]
        texts.append(oracle["rationale"])
    return texts


def test_manifest_is_frozen_and_counts_are_balanced():
    digest = hashlib.sha256(MANIFEST.read_bytes()).hexdigest()
    recorded = (DATA / "manifest.json.sha256").read_text().split()[0]
    assert digest == recorded
    data = manifest()
    assert data["notice"] == NOTICE and data["synthetic_only"] is True
    entries = data["samples"]
    assert len(entries) == 120
    ids = [e["sample_id"] for e in entries]
    assert len(set(ids)) == 120
    assert Counter(e["category_no"] for e in entries) == {n: 10 for n in range(1, 13)}
    for entry in entries:
        for kind in ("sample", "oracle"):
            raw = (ROOT / entry[f"{kind}_path"]).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == entry[f"{kind}_sha256"], entry["sample_id"]
    on_disk = {p.stem for p in (DATA / "samples").glob("*.json")}
    assert on_disk == {p.stem for p in (DATA / "oracle").glob("*.json")} == set(ids)


def test_samples_parse_and_never_contain_oracle_content():
    for entry, sample, oracle in pairs():
        assert entry["sample_path"].startswith("evals/live_v1/samples/")
        assert entry["oracle_path"].startswith("evals/live_v1/oracle/")
        assert set(sample) == {"sample_id", "form", "request"}
        assert sample["form"] in {"text", "chat", "markdown_table"}
        request = sample["request"]
        CreateReview.model_validate({**request, "request_key": sample["sample_id"]})
        assert request["snapshot"]["synthetic"] is True
        blob = json.dumps(sample, ensure_ascii=False)
        assert not ORACLE_KEYS & set(re.findall(r'"(\w+)":', blob)), entry["sample_id"]
        if oracle["rationale"]:
            assert oracle["rationale"] not in blob
        for proposal in oracle["allowed_proposals"]:
            for question in proposal["questions"]:
                assert question not in blob


def test_oracles_use_only_contract_operations_and_reproduce_rule_checks():
    for entry, sample, oracle in pairs():
        request = sample["request"]
        assert oracle["sample_id"] == entry["sample_id"] == sample["sample_id"]
        assert oracle["category_no"] == entry["category_no"]
        assert oracle["drafted_by"] == "AI"
        if oracle["category_no"] in {6, 7, 8, 12}:
            assert oracle["human_review_status"] == "已复核"
            assert oracle["reviewed_by"] == "小八（非 Qi 人工复核）"
        else:
            assert oracle["human_review_status"] == "小八一致性复核"
            assert oracle["reviewed_by"] == "小八一致性复核"
        snapshot = Snapshot.model_validate(request["snapshot"])
        selected = request["selected_order_id"]
        order = next(o for o in snapshot.orders if o.order_id == selected)
        lines = [t for t in request["instruction"].splitlines() if t.strip()]
        catalog = {item.sku_id for item in snapshot.catalog}
        seen = []
        for raw in oracle["allowed_proposals"]:
            proposal = Proposal.model_validate(raw)
            assert proposal.order_id == selected
            for change in proposal.changes:
                assert change.mode in MODES and change.unit == "件"
                assert change.sku_id in {line.sku_id for line in order.lines}
                assert change.to_sku is None or change.to_sku in catalog
            check = check_proposal(
                snapshot, selected, request["instruction"], proposal, source_texts=lines
            )
            seen.append(
                {
                    "status": check["status"],
                    "rows": [
                        {k: r[k] for k in ("sku_id", "old_qty", "new_qty")} for r in check["rows"]
                    ],
                }
            )
        assert seen[0] == oracle["expected_check"], entry["sample_id"]
        assert oracle["expected_check_variants"] == seen, entry["sample_id"]
        if oracle["necessary_fact_codes"]:
            assert all(p["disposition"] == "clarify" for p in oracle["allowed_proposals"])


@pytest.mark.parametrize("category", sorted(HANDOFF_CATEGORIES))
def test_out_of_contract_categories_are_handoff_without_proposal(category):
    rows = [(e, o) for e, _, o in pairs() if e["category_no"] == category]
    assert len(rows) == 10
    for _, oracle in rows:
        assert oracle["handoff"] is True and oracle["handoff_or_no_proposal"] is True
        assert {p["disposition"] for p in oracle["allowed_proposals"]} <= {"clarify", "no_change"}
        assert all(p["changes"] == [] for p in oracle["allowed_proposals"])
        assert all(v["rows"] == [] for v in oracle["expected_check_variants"])


def test_no_sentence_reused_from_protected_sources():
    protected = protected_texts()
    sentences = {norm(s) for text in protected for s in SPLIT.split(text) if len(norm(s)) >= 6}
    shingles = {
        n[i : i + SHINGLE]
        for n in (norm(text) for text in protected)
        for i in range(max(0, len(n) - SHINGLE + 1))
    }
    assert sentences and shingles
    # Negative control: a protected sentence and a 12-character window must still be detected.
    probe = next(s for text in protected for s in SPLIT.split(text) if len(norm(s)) >= 12)
    assert norm(probe) in sentences
    probe_norm = norm(probe)
    windows = range(len(probe_norm) - SHINGLE + 1)
    assert any(probe_norm[i : i + SHINGLE] in shingles for i in windows)
    for text in our_texts():
        for sentence in SPLIT.split(text):
            assert norm(sentence) not in sentences, sentence
        n = norm(text)
        overlap = [
            n[i : i + SHINGLE]
            for i in range(len(n) - SHINGLE + 1)
            if n[i : i + SHINGLE] in shingles
        ]
        assert not overlap, (text, overlap[:3])
