"""Replay reported development facts, not a reproduction of stochastic model output.

Only two existing dev inputs are included. No oracle or held-out assets are read.
The parent retained parsed proposals, not raw provider requests/responses.
"""

from pathlib import Path

from app.review.contracts import Proposal, Snapshot
from app.review.documents import read_document
from app.review.rules import check_proposal

FIXTURES = Path(__file__).parent / "fixtures" / "review_development"


def read_case(name, extension):
    folder = FIXTURES / name
    snapshot = Snapshot.model_validate_json((folder / "baseline.json").read_bytes())
    path = folder / ("request." + extension)
    document, _ = read_document(path.name, path.read_bytes())
    return snapshot, [span.text for span in document.spans]


def check(snapshot, lines, payload):
    return check_proposal(
        snapshot,
        snapshot.orders[0].order_id,
        "\n".join(lines),
        Proposal.model_validate(payload),
        source_texts=lines,
    )


def change(order, sku, before, after, quantity, citations, *, target=None):
    return {
        "order_id": order,
        "disposition": "propose",
        "questions": [],
        "summary": "Synthetic offline replay, not a new model observation",
        "changes": [
            {
                "mode": "replace_remaining" if target else "set_total",
                "sku_id": sku,
                "to_sku": target,
                "quantity": quantity,
                "unit": "件",
                "before_qty": before,
                "after_qty": after,
                "citations": citations,
            }
        ],
    }


def test_actual_pdf_wrap_keeps_context_and_unchanged_other_line():
    snapshot, lines = read_case("pdf-wrapped", "pdf")
    assert lines[1:] == [
        "SYN-3404，款号00104 藏蓝 M 总量改成39件，白 L",
        "和其他行不变。",
    ]
    reported = {
        "order_id": "SYN-3404",
        "disposition": "clarify",
        "changes": [],
        "questions": ["请确认白L是否变化"],
        "summary": "Reconstructed from reported parsed output",
    }
    assert check(snapshot, lines, reported)["status"] == "clarify"
    payload = change(
        "SYN-3404",
        "00104-藏蓝-M",
        84,
        39,
        39,
        [
            {"line": 2, "quote": lines[1]},
            {"line": 3, "quote": lines[2]},
        ],
    )
    result = check(snapshot, lines, payload)
    assert result["status"] == "ready"
    assert [(r["sku_id"], r["new_qty"]) for r in result["rows"]] == [("00104-藏蓝-M", 39)]
    assert snapshot.orders[0].lines[1].ordered_qty == 45


def test_multi_line_context_does_not_allow_fabricated_single_line_citation():
    snapshot, lines = read_case("pdf-wrapped", "pdf")
    payload = change(
        "SYN-3404",
        "00104-藏蓝-M",
        84,
        39,
        39,
        [
            {"line": 2, "quote": lines[1] + lines[2]},
        ],
    )
    result = check(snapshot, lines, payload)
    assert result["status"] == "blocked"
    assert any("来源无法对照原文行" in issue for issue in result["issues"])


def test_reported_replacement_quantity_still_blocks_wrong_source_total():
    snapshot, lines = read_case("replacement", "txt")
    assert "未发部分中取14件换成白 L，已发10件保持；不是全部换。" in lines[0]
    payload = change(
        "SYN-3512",
        "00212-藏蓝-M",
        90,
        90,
        14,
        [{"line": 1, "quote": lines[0]}],
        target="00212-白-L",
    )
    reported = check(snapshot, lines, payload)
    assert reported["status"] == "blocked"
    assert "提案新数量与数量语义不一致，请纠正" in reported["issues"]
    payload["changes"][0]["after_qty"] = 76
    result = check(snapshot, lines, payload)
    assert result["status"] == "ready"
    assert [(r["sku_id"], r["old_qty"], r["new_qty"]) for r in result["rows"]] == [
        ("00212-白-L", 45, 59),
        ("00212-藏蓝-M", 90, 76),
    ]
    # The target total also cannot be put into the source after_qty field.
    payload["changes"][0]["after_qty"] = 59
    assert check(snapshot, lines, payload)["status"] == "blocked"
