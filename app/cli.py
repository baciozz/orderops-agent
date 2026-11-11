"""Operator-started commands; the bounded worker loop is always FIXTURE-only."""

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy.engine import make_url

from app.adapters.budget import BudgetLedger, reconcile_reservations
from app.adapters.live_model import LiveLLMClient
from app.adapters.llm import validated_live_provider
from app.config import Settings, get_settings
from app.db.schema import init_db
from app.db.session import ensure_sqlite_parent, make_engine, make_session_factory
from app.errors import AppError, LiveStartRefused
from app.observability import configure_logging, exception_code, get_logger, log_event
from app.orchestration.worker_loop import LoopOptions, StopFlag, run_loop, shutdown_signals
from app.services.seed import load_seed


def _check_model_auth(*, probe: bool) -> int:
    # Do not use cached settings or dotenv. Never print exception text, model
    # output, settings validation details, or any supplied configuration values.
    report: dict[str, object] = {
        "status": "AUTH_CONFIGURATION",
        "http_status": None,
        "error_type": None,
        "error_code": None,
        "ledger_spent_usd": None,
    }
    engine = None
    book = None
    try:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        stream = sys.stderr if probe else sys.stdout
        for name, status in (
            ("OOS_LIVE_ENABLED", "有" if settings.live_enabled else "无"),
            ("OOS_LLM_PROVIDER", "合法" if settings.llm_provider == "openai" else "不合法"),
            ("OOS_LLM_MODEL", "有" if settings.llm_model else "无"),
            (
                "OOS_LLM_BASE_URL 官方地址",
                "合法" if settings.has_official_llm_base_url else "不合法",
            ),
            ("OOS_PRICE_IN_PER_MTOK", "有" if settings.price_in_per_mtok is not None else "无"),
            ("OOS_PRICE_OUT_PER_MTOK", "有" if settings.price_out_per_mtok is not None else "无"),
            ("OOS_MODEL_PRICES", "有" if settings.model_prices else "无"),
            (
                "当前模型单价",
                "有" if settings.llm_model and settings.prices_for(settings.llm_model) else "无",
            ),
            ("OOS_LLM_API_KEY", "有" if settings.has_llm_api_key else "无"),
        ):
            print(f"{name}：{status}", file=stream)
        if not settings.has_llm_api_key and "OPENAI_API_KEY" in os.environ:
            print(
                "项目只读 OOS_LLM_API_KEY，请在运行时把同一密钥映射到该变量名",
                file=stream,
            )
        if not probe:
            return 0
        # Official OpenAI only; compatible/local providers are outside this command.
        if settings.llm_provider != "openai":
            raise LiveStartRefused("PROVIDER_UNSUPPORTED")
        validated_live_provider(settings)
        if "OOS_DATABASE_URL" not in os.environ:
            raise AppError("须显式指定持久账本", code="LEDGER_NOT_CONFIGURED")
        url = make_url(settings.database_url)
        if (
            url.drivername != "sqlite"
            or not url.database
            or url.database == ":memory:"
            or url.database.startswith("file:")
            or url.query
        ):
            raise AppError("须使用文件 SQLite 账本", code="LEDGER_NOT_PERSISTENT")
        ensure_sqlite_parent(settings.database_url)
        engine = make_engine(settings.database_url)
        init_db(engine)
        book = BudgetLedger.from_settings(
            make_session_factory(engine),
            settings,
            cap_usd=settings.effective_live_budget_usd,
        )
        if book.spent() >= book.cap_usd:
            raise AppError("账本累计费用已达上限", code="BUDGET_EXCEEDED")
        report["http_status"] = LiveLLMClient(settings, book, trust_env=True).check_auth()
        report["status"] = "ok"
    except AppError as exc:
        report["status"] = exception_code(exc)
        # These fields were sanitized by the model transport. No upstream text.
        report["http_status"] = exc.details.get("status_code")
        report["error_type"] = exc.details.get("error_type")
        report["error_code"] = exc.details.get("error_code")
        if report["status"] == "MODEL_AUTH":
            report["next_step"] = (
                "检查密钥是否被撤销、项目/组织权限、变量名是否为 OOS_LLM_API_KEY、"
                "网络是否放行 api.openai.com:443"
            )
    except Exception:
        report["status"] = "AUTH_CONFIGURATION"
    finally:
        try:
            if book is not None:
                spent = book.spent()
                report["ledger_spent_usd"] = format(spent, "f")
                if spent > book.cap_usd:
                    report["status"] = "BUDGET_EXCEEDED"
        except Exception:
            report["status"] = "AUTH_LEDGER"
        finally:
            if engine is not None:
                try:
                    engine.dispose()
                except Exception:
                    report["status"] = "AUTH_LEDGER"
    if probe:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print(report["status"])
    return 0 if report["status"] == "ok" else 2


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
    auth = sub.add_parser("check-model-auth", help="本地检查模型配置；仅 --probe 发送一次请求")
    auth.add_argument("--probe", action="store_true", help="使用持久预算账本发送一次合成认证请求")
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
    if args.command == "check-model-auth":
        return _check_model_auth(probe=args.probe)
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
                "限定字段核验完成；整单来源与共享依赖仍不确定，非全库通过；业务提交未重复执行"
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
