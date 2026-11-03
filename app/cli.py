"""Command line: init-db and seed. No network."""

import argparse

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
        if args.command == "worker-once":
            from app.orchestration.outbox import publish_pending
            from app.orchestration.watchdog import scan
            from app.orchestration.worker import run_once

            factory = make_session_factory(engine)
            scan(factory)
            run_once(factory)
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
