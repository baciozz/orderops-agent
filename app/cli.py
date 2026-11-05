"""Command line: init-db and seed. No network."""

import argparse
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from app.adapters.budget import reconcile_reservations
from app.config import get_settings
from app.db.schema import init_db
from app.db.session import ensure_sqlite_parent, make_engine, make_session_factory
from app.observability import configure_logging
from app.services.seed import load_seed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="oos", description="order-ops-superagent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="创建 SQLite 表")
    sub.add_parser("seed", help="载入黄金演示的合成数据")
    sub.add_parser("worker-once", help="恢复任务，处理一条 fixture 任务并投递 outbox")
    sub.add_parser("reconcile-budget", help="标记过期模型预算预留，保留全部悲观费用")
    verify = sub.add_parser(
        "verify-operation", help="对已转人工的操作执行一次只读核验，不重提或修复业务"
    )
    verify.add_argument("operation_id", help="已提交的 Operation ID")
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings)
    ensure_sqlite_parent(settings.database_url)
    engine = make_engine(settings.database_url)
    try:
        init_db(engine)
        if args.command == "init-db":
            print(f"已创建表：{settings.database_url}")
            return 0
        if args.command == "reconcile-budget":
            count = reconcile_reservations(
                make_session_factory(engine),
                now=datetime.now(UTC),
                ttl=timedelta(seconds=settings.effective_budget_reservation_ttl_s),
            )
            print(f"已标记过期预算预留：{count}；费用和调用次数保持占用")
            return 0
        if args.command == "verify-operation":
            from app.errors import AppError
            from app.services.verification import verify_manually

            try:
                passed = verify_manually(
                    make_session_factory(engine), args.operation_id, settings=settings
                )
            except AppError as exc:
                print(f"核验未执行：{exc.code}")
                return 2
            print(
                "只读核验通过；业务提交未重复执行"
                if passed
                else "核验仍未通过，保留人工处理状态；未修改业务数据"
            )
            return 0 if passed else 1
        if args.command == "worker-once":
            from app.orchestration.outbox import publish_pending
            from app.orchestration.watchdog import scan
            from app.orchestration.worker import DEFAULT_LIMITS, Limits, effective_active, run_once

            factory = make_session_factory(engine)
            scan(factory, settings=settings)
            limits: Limits = replace(
                DEFAULT_LIMITS, active=effective_active(settings.worker_max_active)
            )
            run_once(factory, limits=limits)
            publish_pending(factory)
            return 0
        report = load_seed(make_session_factory(engine), database_url=settings.database_url)
    finally:
        engine.dispose()
    if report.created:
        print(f"已载入合成 seed：{', '.join(report.orders)} @ {report.database_url}")
    else:
        print(f"seed 已存在，未重复写入：{', '.join(report.orders)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
