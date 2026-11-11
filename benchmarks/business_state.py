"""Read-only full-row digest for an isolated synthetic SQLite evaluation database."""

import hashlib
import json


def business_state(sessions):
    """Call before/after with unrelated writers stopped; return no row contents.

    Includes newly added user tables, not just the ORM's known tables. Expected
    review-workspace/billing writes and migration/internal metadata are excluded.
    A matching row count alone is deliberately not sufficient evidence.
    """
    excluded = {"review_workspace", "model_usage", "alembic_version"}
    tables = []
    with sessions() as session:
        connection = session.connection()
        names = sorted(
            connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).scalars()
        )
        for name in names:
            if name in excluded:
                continue
            quoted = '"' + name.replace('"', '""') + '"'
            result = connection.exec_driver_sql("SELECT * FROM " + quoted)
            columns = list(result.keys())
            encoded = []
            for row in result:
                values = [{"bytes_hex": v.hex()} if isinstance(v, bytes) else v for v in row]
                encoded.append(json.dumps(values, ensure_ascii=False, separators=(",", ":")))
            encoded.sort()
            table = {"table": name, "columns": columns, "rows": encoded}
            tables.append(
                {
                    "table": name,
                    "row_count": len(encoded),
                    "sha256": hashlib.sha256(
                        json.dumps(table, sort_keys=True).encode()
                    ).hexdigest(),
                }
            )
    return {
        "digest_scope": "all_business_tables_v1",
        "tables": tables,
        "sha256": hashlib.sha256(json.dumps(tables, sort_keys=True).encode()).hexdigest(),
        "not_attribution_proof": True,
    }
