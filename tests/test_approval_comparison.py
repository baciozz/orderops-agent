"""Read-only real TaskView -> Jinja checks. No server, browser or IPC needed."""

from copy import deepcopy
from pathlib import Path

import pytest
from app.config import Settings
from app.db.models import Plan, PurchaseOrder
from app.orchestration.fixtures import TEXT_ONLY_UNCUT
from app.orchestration.inbox import accept_input
from app.orchestration.state import utcnow
from app.orchestration.worker import run_once
from app.services.seed import load_seed
from app.services.task_view import build_task_view
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import MetaData, select

from test_mto_preservation import execute, plan
from test_mto_preservation import split as split_fixture

split = split_fixture

TEMPLATES = Path(__file__).resolve().parents[1] / "app/web/templates"


@pytest.fixture(scope="session", autouse=True)
def _sandbox_asyncio_polling():
    """Synchronous template tests do not require the unrelated IPC fixture."""
    yield


def render(view, **kwargs):
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape())
    return env.get_template("task_view.html").render(
        view=view,
        plan_id="plan",
        approval_id=kwargs.get("approval_id"),
        renewable=kwargs.get("renewable", False),
        uploaded_sources=[],
        can_answer=False,
        can_cancel=False,
    )


def read_view(factory, task_id):
    with factory() as session:
        return build_task_view(
            session, task_id, request_id="test", correlation_id="test", now=utcnow()
        ).model_dump(mode="json")


def all_tables(engine):
    metadata = MetaData()
    metadata.reflect(bind=engine)
    with engine.connect() as connection:
        return {
            name: sorted(repr(tuple(row)) for row in connection.execute(select(table)))
            for name, table in metadata.tables.items()
        }


@pytest.fixture()
def actual(session_factory, database_url):
    load_seed(session_factory, database_url=database_url)
    _, task_id, _ = accept_input(
        session_factory,
        thread_id="comparison",
        client_event_key="comparison",
        text=TEXT_ONLY_UNCUT,
        selected_order_id="SO-1001",
    )
    run_once(
        session_factory,
        settings=Settings(
            _env_file=None,
            database_url=database_url,
            live_enabled=False,
        ),
    )
    return read_view(session_factory, task_id)


def test_actual_fixture_read_only_render(actual, engine):
    before = all_tables(engine)
    html = render(actual)
    assert all_tables(engine) == before
    for marker in [
        "FAB-BLACK",
        "FAB-NAVY",
        "360 米（m）",
        "180 件",
        "120 件",
        "待供应商确认（PENDING_SUPPLIER）",
        "内容不变，仅重新关联",
        "历史快照",
        "当前读取：状态",
        "库存未冻结",
        "当前可用库存",
        "已开裁",
    ]:
        assert marker in html
    purchase = html.split("PUR-3002 · 历史计划快照")[1].split("</section>")[0]
    assert purchase.index("FAB-BLACK") < purchase.index("FAB-NAVY")
    assert "当前读取：状态 采购记录已建立（PLACED）" in purchase
    assert "关联订单版本 1" in purchase
    assert "当前批准条件下可执行：是（true）" in purchase
    assert "<td>未提供</td>" in html  # MTO is not zero inventory.
    assert "<td>0</td>" in html


@pytest.mark.parametrize(
    "qty,received", [(300, False), (301, False), (299, False), (301, True), (120, False)]
)
def test_split_real_plans_and_committed_history(split, engine, qty, received):
    factory, request = split
    if received:
        with factory.begin() as session:
            row = session.get(PurchaseOrder, "PUR-3002")
            row.received_qty, row.status = 180, "RECEIVED"
    changes = [{"mode": "set_total", "sku_id": "C-BLACK-L", "qty": qty}]
    if qty == 300:
        changes = [
            {"mode": "replace_remaining", "from_sku": "A-BLACK-M", "to_sku": "A-WHITE-M", "qty": 1}
        ]
    preview = plan(request, changes)
    assert preview.status == "VALIDATED"
    with factory() as session:
        task_id = session.get(Plan, preview.plan_id).task_id
    before = all_tables(engine)
    view = read_view(factory, task_id)
    html = render(view)
    assert all_tables(engine) == before
    for doc in view["downstream"]:
        assert doc["doc_id"] in html
        assert str(doc["after"]["status"]) in html
    if qty == 300:
        assert all(doc["verdict"] == "UNAFFECTED_RELINK" for doc in view["downstream"])
    if received:
        assert "新建采购草稿" in html and "2 米（m）" in html
    if qty == 120:
        assert "取消草稿" in html and "不代表当前单据已取消" in html
    execute(request, preview)
    committed = read_view(factory, task_id)
    assert committed["downstream"][0]["before"] == view["downstream"][0]["before"]
    html = render(committed)
    assert "历史计划快照" in html and 'id="approve" disabled' in html
    assert "当前批准条件下可执行：否（false）" in html


def test_missing_false_zero_and_untrusted_fields(actual):
    view = deepcopy(actual)
    doc = view["downstream"][-1]
    doc["before"] = {
        "material_id": '<script>alert("x")</script>',
        "ordered_qty": 0,
        "received_qty": False,
        "status": {"secret": "NESTED_SECRET"},
        "unknown": "UNKNOWN_SECRET",
        "text_excerpt": "EXCERPT_SECRET",
    }
    doc["after"] = None
    doc["status"] = '<img src=x onerror="x">'
    doc["linked_revision"] = 0
    doc["executable"] = False
    html = render(view)
    assert "&lt;script&gt;" in html and "&lt;img" in html
    assert "<script>alert" not in html and "<img src=x" not in html
    assert not any(
        secret in html for secret in ["NESTED_SECRET", "UNKNOWN_SECRET", "EXCERPT_SECRET"]
    )
    assert "0 米（m）" in html and "否（false）" in html
    assert "未提供（非标量）" in html and "不据此判断已取消" in html
    assert "关联订单版本 0" in html
    doc["before"] = None
    doc["doc_type"] = "unknown"
    doc["after"] = {"material_id": "DO_NOT_SHOW"}
    html = render(view)
    assert "未知单据类型，未展示快照字段" in html and "DO_NOT_SHOW" not in html
    assert "不据此判断原单是否存在" in html
    assert "新建采购草稿" not in html


@pytest.mark.parametrize(
    "approvable,approval_id,renewable",
    [
        (True, None, False),
        (True, "approval", False),
        (False, "approval", False),
        (False, None, True),
    ],
)
def test_action_controls_unchanged(actual, approvable, approval_id, renewable):
    actual["approval"]["approvable"] = approvable
    html = render(actual, approval_id=approval_id, renewable=renewable)
    assert ('id="approve" disabled' in html) == (not approvable)
    assert ('id="commit" disabled' in html) == (not approval_id or not approvable)
    assert ('id="renew"' in html) == renewable
    assert 'data-plan-id="plan"' in html
    assert f'data-task-id="{actual["card"]["task_id"]}"' in html
    assert f'data-plan-hash="{actual["approval"]["plan_hash"]}"' in html
    assert f'data-intent-revision="{actual["card"]["intent_revision"]}"' in html
    assert f'data-approval-id="{approval_id or ""}"' in html
    actual["line_diffs"], actual["downstream"] = [], []
    assert "下游影响清单" in render(actual)


def test_current_facts_do_not_replace_historical_snapshots(actual, session_factory, engine):
    task_id = actual["card"]["task_id"]
    with session_factory.begin() as session:
        row = session.get(PurchaseOrder, "PUR-3002")
        row.status, row.received_qty = "PARTIALLY_RECEIVED", 10
        row.doc_revision += 1
    before = all_tables(engine)
    changed = read_view(session_factory, task_id)
    assert changed["approval"]["disabled_reason"] == "STALE"
    doc = changed["downstream"][-1]
    assert doc["before"] == actual["downstream"][-1]["before"]
    assert doc["after"] == actual["downstream"][-1]["after"]
    html = render(changed)
    assert "当前读取：状态 部分收货（PARTIALLY_RECEIVED）" in html
    assert "待供应商确认（PENDING_SUPPLIER）" in html
    assert "0 米（m）" in html
    assert 'id="approve" disabled' in html
    assert all_tables(engine) == before


def test_every_allowlisted_snapshot_value_is_rendered(actual):
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape())
    template = env.get_template("approval_comparison.html")
    for doc in actual["downstream"]:
        html = template.render(view={"line_diffs": [], "downstream": [doc]})
        for side in ["before", "after"]:
            for key, val in doc[side].items():
                if key in {"sku_id", "material_id", "status", "change_state"}:
                    assert str(val) in html
                elif key in {"planned_qty", "cut_qty"}:
                    assert f"{val} 件" in html
                elif key in {"ordered_qty", "received_qty"}:
                    assert f"{val} 米（m）" in html
