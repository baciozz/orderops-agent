"""Measure readback lock cost on fresh synthetic SQLite databases only.

Run from a development checkout: python scripts/measure_verification.py --samples 3
No customer database, external service, or model is used. Times are diagnostic, not SLAs.
"""

import argparse
import json
import logging
import sys
from contextlib import contextmanager
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.db.models import Customer, Operation  # noqa: E402
from app.db.schema import init_db  # noqa: E402
from app.db.session import (  # noqa: E402
    _BEGIN_IMMEDIATE,
    begin_immediate,
    make_engine,
    make_session_factory,
)
from app.services import kernel  # noqa: E402
from app.services.seed import load_seed  # noqa: E402
from app.services.verification import retry_pending  # noqa: E402
from sqlalchemy import event, select, text  # noqa: E402
from tests.verification_scale import NOW, add_orders  # noqa: E402


def measure(kind, count, samples, *, noise=True, link_index=False):
    with TemporaryDirectory(prefix="verification-scale-") as directory:
        url = f"sqlite:///{directory}/synthetic.db"
        engine = make_engine(url)
        init_db(engine)
        factory = make_session_factory(engine)
        load_seed(factory, database_url=url)
        add_orders(factory, count, kind)
        if link_index:
            with factory() as session, begin_immediate(session):
                session.execute(
                    text("CREATE INDEX benchmark_link_order ON downstream_link(order_id)")
                )
        with factory() as session:
            assert kernel.consistency_problems(session) == []
            assert kernel.downstream_problems(session) == []
        original = kernel.downstream_problems
        holds, sql_counts, sql_times = [], [], []
        active, started = [False], [0.0]
        current = [0, 0.0]

        def before_sql(connection, cursor, statement, parameters, context, executemany):
            if active[0]:
                current[0] += 1
                started[0] = perf_counter()

        def after_sql(connection, cursor, statement, parameters, context, executemany):
            if active[0]:
                current[1] += perf_counter() - started[0]

        event.listen(engine, "before_cursor_execute", before_sql)
        event.listen(engine, "after_cursor_execute", after_sql)

        @contextmanager
        def timed_lock(session):
            start = None
            try:
                with begin_immediate(session):
                    session.connection()
                    start = perf_counter()
                    active[0] = True
                    current[:] = [0, 0.0]
                    yield session
            finally:
                active[0] = False
                if start is not None:
                    holds.append((perf_counter() - start) * 1000)
                    sql_counts.append(current[0])
                    sql_times.append(current[1] * 1000)

        def readback(session):
            result = original(session)
            if noise and not _BEGIN_IMMEDIATE.get():
                with factory() as writer, begin_immediate(writer):
                    writer.scalars(select(Customer)).first().display_name += "*"
            return result

        for i in range(samples + 1):
            op_id = f"BENCH-OP-{i}"
            with factory() as session, begin_immediate(session):
                session.add(
                    Operation(
                        id=op_id,
                        idempotency_key=op_id,
                        request_hash="synthetic",
                        status="COMMITTED",
                        result_json={},
                        committed_verification_pending=True,
                    )
                )
            with (
                patch.object(kernel, "begin_immediate", timed_lock),
                patch.object(kernel, "downstream_problems", readback),
            ):
                assert (
                    retry_pending(factory, op_id, now=NOW, settings=Settings(_env_file=None)) == 1
                )
            with factory() as session:
                assert not session.get(Operation, op_id).committed_verification_pending
        engine.dispose()
        return dict(
            kind=kind,
            extra_orders=count,
            samples=samples,
            noise=noise,
            link_index=link_index,
            lock_ms_median=round(median(holds[1:]), 3),
            lock_ms_max=round(max(holds[1:]), 3),
            lock_sql_count=sql_counts[1:],
            sql_execute_ms_median=round(median(sql_times[1:]), 3),
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=3, choices=range(1, 11))
    parser.add_argument(
        "--link-index", action="store_true", help="Index-only experiment in the temporary database"
    )
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    for kind, count in (("stock", 0), ("stock", 100), ("stock", 1000), ("mto", 100), ("mto", 500)):
        print(
            json.dumps(measure(kind, count, args.samples, link_index=args.link_index)), flush=True
        )


if __name__ == "__main__":
    main()
