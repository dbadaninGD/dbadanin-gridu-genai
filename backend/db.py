"""
Postgres access layer.

Everything that touches the database goes through this module and uses
SQLAlchemy's bound parameters (`text(sql).execute(params)` /
`conn.execute(text(sql), rows)`), never raw string interpolation of user- or
model-supplied values. The original implementation built INSERT statements
with f-strings (`VALUES ({', '.join(str(v) for v in row.values())})`), which
is both a SQL-injection hole and broken for anything except plain strings
(NULLs, booleans, numbers, dates all round-trip incorrectly through
`str()` + manual quoting). This module fixes both problems.
"""

from __future__ import annotations

import csv
import io
import zipfile

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ddl_parser import ResolvedSchema, Table, to_postgres_ddl


def create_schema(engine: Engine, resolved: ResolvedSchema) -> None:
    """(re)creates every table in `resolved` in dependency order, dropping
    any previous version first so re-generating is always safe to re-run."""
    statements = to_postgres_ddl(resolved)
    with engine.begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))


def _insert_sql(table_name: str, columns: list[str]) -> str:
    col_list = ", ".join(f'"{c}"' for c in columns)
    param_list = ", ".join(f":{c}" for c in columns)
    return f'INSERT INTO "{table_name}" ({col_list}) VALUES ({param_list})'


def insert_rows(engine: Engine, resolved: ResolvedSchema, data: dict[str, list[dict]]) -> None:
    """Bulk-inserts generated rows for every table, in dependency order, all
    inside a single transaction. Deferred (cyclic) foreign keys were added
    as DEFERRABLE INITIALLY DEFERRED constraints, so Postgres only checks
    them at COMMIT time -- by which point every table's rows exist."""
    with engine.begin() as conn:
        for name in resolved.order:
            rows = data.get(name) or []
            if not rows:
                continue
            columns = list(rows[0].keys())
            conn.execute(text(_insert_sql(name, columns)), rows)


def update_row(engine: Engine, table: Table, pk_value, values: dict) -> None:
    """Updates the non-key columns of a single row, identified by its primary key."""
    pk = table.primary_key
    if not pk or not values:
        return
    assignments = ", ".join(f'"{c}" = :{c}' for c in values)
    sql = f'UPDATE "{table.name}" SET {assignments} WHERE "{pk}" = :__pk'
    params = dict(values)
    params["__pk"] = pk_value
    with engine.begin() as conn:
        conn.execute(text(sql), params)


def fetch_table_rows(engine: Engine, table_name: str) -> list[dict]:
    with engine.connect() as conn:
        result = conn.execute(text(f'SELECT * FROM "{table_name}" ORDER BY 1'))
        return [dict(row._mapping) for row in result]


def get_live_schema_description(conn) -> str:
    """Extracts table/column/type info from the *live* database (used to
    ground the natural-language-to-SQL prompt in `/api/query`)."""
    schema_query = """
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
        ORDER BY table_name, ordinal_position;
    """
    result = conn.execute(text(schema_query)).fetchall()

    # Present identifiers pre-quoted so the NL->SQL model copies them verbatim.
    # Table/column names are mixed-case, and an unquoted mixed-case identifier
    # is folded to lowercase by PostgreSQL and won't match, so the quotes are
    # load-bearing, not cosmetic.
    schema_dict: dict[str, list[str]] = {}
    for table, col, dtype in result:
        schema_dict.setdefault(table, []).append(f'"{col}" ({dtype})')

    return "\n\n".join(f'Table "{table}"\nColumns: {", ".join(cols)}' for table, cols in schema_dict.items())


_READ_ONLY_PREFIXES = ("SELECT", "WITH")


def is_read_only_query(sql: str) -> bool:
    """Guards `/api/query` against a model-generated statement that isn't a
    plain read: the original implementation executed whatever SQL the LLM
    produced with no check at all."""
    return sql.strip().upper().startswith(_READ_ONLY_PREFIXES)


def build_zip_of_all_tables(tables: dict[str, list[dict]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for table_name, rows in tables.items():
            csv_buffer = io.StringIO()
            if rows:
                writer = csv.DictWriter(csv_buffer, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)
            zf.writestr(f"{table_name}.csv", csv_buffer.getvalue())
    return buffer.getvalue()
