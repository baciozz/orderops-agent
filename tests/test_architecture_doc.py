"""Offline checks for architecture references and the complete task state graph."""

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from app.orchestration.state import ALLOWED

ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = ROOT / "ARCHITECTURE.md"
PREFIXES = ("app/", "docs/", "scripts/", "tests/", "schemas/", "alembic/")


def _document() -> str:
    return DOCUMENT.read_text(encoding="utf-8")


def test_backtick_paths_exist():
    paths = [
        re.split(r"::|:\d+", value, maxsplit=1)[0]
        for value in re.findall(r"(?<!`)`([^`\n]+)`(?!`)", _document())
        if value.startswith(PREFIXES)
    ]
    assert paths, "架构文档必须引用实现文件"
    for path in paths:
        assert (ROOT / path).exists(), f"不存在的代码路径：{path}"


def test_relative_markdown_links_exist():
    targets = re.findall(r"\[[^\]\n]*\]\(\s*(<[^>]+>|[^\s)]+)(?:\s+[^)]*)?\)", _document())
    assert targets, "架构文档必须提供阅读链接"
    for target in targets:
        url = urlsplit(target.removeprefix("<").removesuffix(">"))
        if url.scheme or url.netloc:
            continue
        path = unquote(url.path)
        assert (ROOT / path).exists(), f"不存在的相对链接：{target}"


def test_mermaid_diagrams_and_exact_allowed_transitions():
    blocks = re.findall(r"^```mermaid\s*\n(.*?)^```\s*$", _document(), re.MULTILINE | re.DOTALL)
    assert len(blocks) >= 3
    assert any(re.search(r"^\s*(?:flowchart|graph)\b", block) for block in blocks)
    assert any(re.search(r"^\s*sequenceDiagram\b", block) for block in blocks)
    state_blocks = [block for block in blocks if re.search(r"^\s*stateDiagram-v2\b", block)]
    assert state_blocks, "缺少任务状态图"
    transitions = set()
    for block in state_blocks:
        for line in block.splitlines():
            line = line.split("%%", 1)[0]
            match = re.fullmatch(r"\s*(\[\*\]|\w+)\s*-->\s*(\[\*\]|\w+)(?:\s*:[^\n]*)?\s*", line)
            if match:
                source, target = match.groups()
                if "[*]" not in (source, target):
                    transitions.add((source, target))
    expected = {(source, target) for source, targets in ALLOWED.items() for target in targets}
    assert transitions == expected, (
        f"图中多出的转换：{sorted(transitions - expected)}；"
        f"图中缺少的转换：{sorted(expected - transitions)}"
    )
