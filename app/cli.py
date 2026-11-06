"""Operator-started commands; the bounded worker loop is always FIXTURE-only."""

import argparse
from datetime import UTC, datetime, timedelta

from app.adapters.budget import reconcile_reservations
from app.config import get_settings
from app.db.schema import init_db
from app.db.session import ensure_sqlite_parent, make_engine, make_session_factory
from app.observability import configure_logging, exception_code, get_logger, log_event
from app.orchestration.worker_loop import LoopOptions, StopFlag, run_loop, shutdown_signals
from app.services.seed import load_seed


def _worker_loop(options: LoopOptions) -> int:
    stop = StopFlag()
    try:
        with shutdown_signals(stop):
            settings = get_settings()
            configure_logging(settings)
            ensure_sqlite_parent(settings.database_url)
            engine = make_engine(settings.database_url)
            try:
                init_db(engine)
                result = run_loop(
                    make_session_factory(engine), settings, options=options, stop=stop
                )
            finally:
                engine.dispose()
    except Exception as exc:
        # Include setup/cleanup failures, without exposing exception text or tracebacks.
        log_event(
            get_logger("cli"),
            "worker_loop.cli_failed",
            mode="FIXTURE",
            outcome="failed",
            error_code=exception_code(exc),
        )
        print("FIXTURE worker-loop 异常退出；请检查结构化日志，未自动重试")
        return 1
    print(f"FIXTURE worker-loop 已停止：{result.reason}；已完成 {result.iterations} 轮")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except Exception as exc:
        log_event(get_logger("cli"), "cli.failed", outcome="failed", error_code=exception_code(exc))
        print("命令未完成，请检查配置、数据库和结构化错误码；未自动重试")
        return 1


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="oos", description="order-ops-superagent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="创建 SQLite 表")
    sub.add_parser("seed", help="载入黄金演示的合成数据")
    sub.add_parser("worker-once", help="恢复任务，处理一条 fixture 任务并投递 outbox")
    loop = sub.add_parser("worker-loop", help="显式运行有界 FIXTURE 循环，不自动批准或提交")
    loop.add_argument("--interval-seconds", type=float, default=5.0, help="轮间等待秒数，0.1–3600")
    loop.add_argument("--max-iterations", type=int, default=60, help="最多轮数，1–10000")
    sub.add_parser("reconcile-budget", help="标记过期模型预算预留，保留全部悲观费用")
    verify = sub.add_parser(
        "verify-operation", help="对已转人工的操作执行一次只读核验，不重提或修复业务"
    )
    verify.add_argument("operation_id", help="已提交的 Operation ID")
    audit = sub.add_parser("audit-links", help="只读审计一个提交的历史关联证据，不放行业务")
    audit.add_argument("operation_id", help="需要审计的 Operation ID")
    execution = sub.add_parser("audit-execution", help="只读核对保留的批准执行字段，明确未覆盖范围")
    execution.add_argument("operation_id", help="需要核对的 Operation ID")
    args = parser.parse_args(argv)
    if args.command == "audit-execution":
        from app.services.execution_audit import audit_execution

        execution_report = audit_execution(get_settings().database_url, args.operation_id)
        print(execution_report.model_dump_json())
        return {"consistent_for_retained_fields": 0, "findings": 1, "uncertain": 2}[
            execution_report.status
        ]
    if args.command == "audit-links":
        from app.services.link_audit import audit_operation_links

        link_report = audit_operation_links(get_settings().database_url, args.operation_id)
        print(link_report.model_dump_json())
        return {"consistent_for_operation": 0, "findings": 1, "uncertain": 2}[link_report.status]
    loop_options = None
    if args.command == "worker-loop":
        try:
            loop_options = LoopOptions(args.interval_seconds, args.max_iterations)
        except ValueError as exc:
            parser.error(str(exc))
    if loop_options is not None:
        return _worker_loop(loop_options)
    settings = get_settings()
    configure_logging(settings)
    ensure_sqlite_parent(settings.database_url)
    engine = make_engine(settings.database_url)
    try:
        init_db(engine)
        if args.command == "init-db":
            print("数据库表已初始化")
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
            from app.orchestration.worker_cycle import run_cycle

            run_cycle(make_session_factory(engine), settings)
            return 0
        report = load_seed(make_session_factory(engine), database_url=settings.database_url)
    finally:
        engine.dispose()
    if report.created:
        print(f"已载入合成 seed：{', '.join(report.orders)}")
    else:
        print(f"seed 已存在，未重复写入：{', '.join(report.orders)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
