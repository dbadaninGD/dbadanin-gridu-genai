"""
Synthetic Data Engine -- FastAPI backend.

Endpoints:
  POST /api/generate      Parse a DDL schema, (re)create it in Postgres, and
                           generate + store constraint-aware synthetic data.
  POST /api/refine        Apply a free-text edit instruction to one table's
                           previously generated rows (and persist the result).
  POST /api/query         Natural-language question -> read-only SQL -> rows.
  GET  /api/download/all  A ZIP of every table currently in the database, as CSV.
  GET  /healthz           Liveness check used by docker-compose / GCP.

See ddl_parser.py and generator.py for the schema-parsing and
constraint-aware generation logic this module orchestrates, and db.py for
the (parameterized, injection-safe) database access.
"""

from __future__ import annotations

import json
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import create_engine

import db

# --------------------------------------------------------------------------
# Langfuse tracing -- optional.
#
# Langfuse is only wired up when both keys are present. Otherwise `observe`
# is a plain pass-through decorator, so a keyless deploy produces no tracing
# and, importantly, none of the "Failed to export span batch code: 401"
# noise the SDK logs when it tries to ship spans with no valid credentials.
#
# (Langfuse's Python SDK v3+ moved the @observe decorator to the top-level
# package -- it used to be `langfuse.decorators.observe` in v2. The
# Pipfile.lock here resolves langfuse==4.x, which uses the newer API.)
# --------------------------------------------------------------------------
_LANGFUSE_ENABLED = bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))

if _LANGFUSE_ENABLED:
    from langfuse import observe
else:
    def observe(*_args, **_kwargs):
        def decorator(func):
            return func
        return decorator

from ddl_parser import DDLParseError, Table, parse_ddl, resolve_table_order
from generator import GenerationContext, generate_all_tables, generated_columns

# --------------------------------------------------------------------------
# Configuration / clients
# --------------------------------------------------------------------------

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "default-project")
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
DB_URL = os.getenv("DATABASE_URL", "postgresql://postgres:password@db:5432/datagen_db")
GENERATION_MODEL = os.getenv("GENERATION_MODEL", "gemini-2.5-pro")
REFINE_MODEL = os.getenv("REFINE_MODEL", "gemini-2.5-flash")
QUERY_MODEL = os.getenv("QUERY_MODEL", "gemini-2.5-pro")
MAX_ROWS_PER_TABLE = 1000

# The per-batch output-token budget for data generation. This is deliberately
# NOT the UI "Max Tokens" slider: that slider is a hint, and a low value (its
# old 2048 default) truncates a wide table's rows mid-JSON. Generation always
# gets at least this many tokens so a full batch fits; a caller asking for
# more via the slider raises it further (see generate_data).
GENERATION_MIN_OUTPUT_TOKENS = int(os.getenv("GENERATION_MIN_OUTPUT_TOKENS", "8192"))

app = FastAPI(title="Synthetic Data Engine")
engine = create_engine(DB_URL)

# Caches the most recently generated schema so /api/refine and
# /api/download/all know each table's primary key / foreign key columns
# without the caller having to re-upload the DDL. This is an intentional
# simplification: the app is single-session/single-user by design (no auth,
# no multi-tenancy anywhere else in the stack either).
_SCHEMA_CACHE: dict[str, Table] = {}


def _get_genai_client():
    """Lazily creates the Vertex AI GenAI client so importing this module
    (e.g. from tests) never requires real GCP credentials."""
    from google import genai

    try:
        return genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)
    except Exception as e:  # pragma: no cover - depends on local gcloud auth
        print(f"Warning: GenAI Client initialization failed. Check credentials. {e}")
        return None


def _model_error_detail(e: Exception) -> str:
    """Extracts a concise, human-readable message from a GenAI/Vertex error
    (which carries `.message`/`.code`) so the UI shows e.g.
    '403: Permission ... denied on ... gemini-2.5-pro' instead of a bare 500."""
    message = getattr(e, "message", None) or str(e)
    code = getattr(e, "code", None)
    return f"{code}: {message}" if code else message


# --------------------------------------------------------------------------
# Request / response models
# --------------------------------------------------------------------------

class GenerateRequest(BaseModel):
    prompt: str = ""
    ddl_schema: str
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, ge=64, le=32768)
    rows_per_table: int = Field(default=50, ge=1, le=MAX_ROWS_PER_TABLE)


class RefineRequest(BaseModel):
    table_name: str
    instructions: str
    current_data: list[dict]


class QueryRequest(BaseModel):
    question: str


# --------------------------------------------------------------------------
# GenAI call wrappers (each individually @observe'd for Langfuse tracing)
# --------------------------------------------------------------------------

@observe(name="generate_table_batch", as_type="generation")
def _call_gemini_for_rows(prompt: str, temperature: float, max_tokens: int) -> str:
    from google.genai import types

    client = _get_genai_client()
    if client is None:
        raise HTTPException(status_code=500, detail="GenAI client not initialized. Check GCP credentials.")

    config = types.GenerateContentConfig(
        temperature=temperature,
        max_output_tokens=max_tokens,
        response_mime_type="application/json",
    )
    response = client.models.generate_content(model=GENERATION_MODEL, contents=[prompt], config=config)
    return response.text


@observe(name="refine_table_data", as_type="generation")
def _call_gemini_for_refine(instructions: str, editable_rows: list[dict]) -> list[dict]:
    from google.genai import types

    client = _get_genai_client()
    if client is None:
        raise HTTPException(status_code=500, detail="GenAI client not initialized. Check GCP credentials.")

    prompt = (
        "Modify this JSON array of rows based on these instructions: "
        f"'{instructions}'.\nReturn ONLY the modified JSON array, same length and same keys "
        f"as the input, no extra commentary.\nData:\n{json.dumps(editable_rows)}"
    )
    config = types.GenerateContentConfig(response_mime_type="application/json")
    response = client.models.generate_content(model=REFINE_MODEL, contents=[prompt], config=config)
    result = json.loads(response.text)
    if not isinstance(result, list):
        raise HTTPException(status_code=502, detail="Refine model did not return a JSON array.")
    return result


@observe(name="nl_to_sql_query", as_type="generation")
def _call_gemini_for_sql(question: str, schema_context: str, prior_error: str | None = None) -> str:
    from google.genai import types

    client = _get_genai_client()
    if client is None:
        raise HTTPException(status_code=500, detail="GenAI client not initialized. Check GCP credentials.")

    system_instruction = f"""You are a PostgreSQL expert. Given the following database schema, translate the
user's natural language question into a valid, read-only SQL SELECT query.

Rules:
- Table and column names are case-sensitive and MUST be wrapped in double
  quotes exactly as written in the schema below (e.g. SELECT * FROM
  "Customers" WHERE "city" = 'Boston'). An unquoted mixed-case name like
  Customers is folded to lowercase by PostgreSQL and will fail.
- String literal values use single quotes.
- Do NOT use a table alias (like T1) unless you also declare it in the FROM
  or JOIN clause (e.g. FROM "Customers" AS c ... c."city"). For a
  single-table query, reference columns without any table prefix.

Return ONLY the raw SQL string. Do not include markdown formatting, backticks, or explanations.

Schema:
{schema_context}"""

    contents = [question]
    if prior_error:
        # Self-correction pass: hand the model back its own failed SQL and the
        # database error so it can fix it, instead of failing the request.
        contents = [
            f"{question}\n\nYour previous SQL failed with this PostgreSQL error. "
            f"Return a corrected query.\n{prior_error}"
        ]

    config = types.GenerateContentConfig(
        system_instruction=[system_instruction],
        temperature=0.1,
        max_output_tokens=500,
    )
    response = client.models.generate_content(model=QUERY_MODEL, contents=contents, config=config)
    sql_query = response.text.strip()
    if sql_query.startswith("```"):
        sql_query = sql_query.strip("`")
        if sql_query.lower().startswith("sql"):
            sql_query = sql_query[3:]
    return sql_query.strip()


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/api/generate")
def generate_data(req: GenerateRequest):
    """Parses the uploaded DDL, (re)creates it in Postgres, generates
    `rows_per_table` constraint-respecting rows per table, and stores them."""
    try:
        tables = parse_ddl(req.ddl_schema)
    except DDLParseError as e:
        raise HTTPException(status_code=400, detail=str(e))

    resolved = resolve_table_order(tables)

    try:
        db.create_schema(engine, resolved)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create schema in Postgres: {e}")

    # Generation gets a generous output-token budget regardless of the UI
    # slider, so a wide table's rows never get truncated mid-JSON. The slider
    # can only raise it, never lower it below the floor.
    gen_tokens = max(req.max_tokens, GENERATION_MIN_OUTPUT_TOKENS)

    ctx = GenerationContext(
        rows_per_table=req.rows_per_table,
        temperature=req.temperature,
        max_output_tokens=gen_tokens,
        extra_instructions=req.prompt,
    )

    def call_model(prompt: str) -> str:
        return _call_gemini_for_rows(prompt, req.temperature, gen_tokens)

    try:
        data = generate_all_tables(resolved, ctx, call_model)
    except HTTPException:
        raise
    except (ValueError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=502, detail=f"Model returned data that could not be parsed: {e}")
    except Exception as e:
        # Vertex AI / GenAI errors (auth, quota, model availability, ...) --
        # surface the real message instead of a bare 500 so it reaches the UI.
        raise HTTPException(status_code=502, detail=f"Data generation failed: {_model_error_detail(e)}")

    try:
        db.insert_rows(engine, resolved, data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to store generated data: {e}")

    _SCHEMA_CACHE.clear()
    _SCHEMA_CACHE.update(resolved.tables)

    return {"status": "success", "data": data}


@app.post("/api/refine")
def refine_data(req: RefineRequest):
    """Applies a free-text edit to one table's rows. Primary key and foreign
    key columns are stripped before the request reaches the model (so a
    quick edit can never corrupt referential integrity) and re-attached to
    the response; the update is also persisted back to Postgres so
    'Talk to your data' reflects the edit."""
    table = _SCHEMA_CACHE.get(req.table_name)
    if table is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown table '{req.table_name}'. Generate data before refining it.",
        )

    protected_cols = {fk.column for fk in table.foreign_keys}
    if table.primary_key:
        protected_cols.add(table.primary_key)

    editable_rows = []
    protected_values = []
    for row in req.current_data:
        protected_values.append({k: v for k, v in row.items() if k in protected_cols})
        editable_rows.append({k: v for k, v in row.items() if k not in protected_cols})

    refined = _call_gemini_for_refine(req.instructions, editable_rows)

    if len(refined) != len(protected_values):
        raise HTTPException(
            status_code=502,
            detail="Refine model returned a different number of rows than it was given.",
        )

    merged_rows = []
    for refined_row, protected in zip(refined, protected_values):
        merged = dict(refined_row)
        merged.update(protected)  # protected columns always win
        merged_rows.append(merged)

    if table.primary_key:
        for row in merged_rows:
            editable = {k: v for k, v in row.items() if k not in protected_cols}
            try:
                db.update_row(engine, table, row[table.primary_key], editable)
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Failed to persist refined data: {e}")

    return {"status": "success", "data": merged_rows}


@app.post("/api/query")
def query_database(req: QueryRequest):
    """Translates a natural-language question into a read-only SQL query,
    executes it, and returns the rows."""
    from sqlalchemy import text

    with engine.connect() as conn:
        schema_context = db.get_live_schema_description(conn)
        if not schema_context:
            raise HTTPException(status_code=400, detail="No data has been generated yet.")

        # Up to two attempts: generate SQL, run it, and if it fails to execute,
        # hand the model its own SQL + the PostgreSQL error once so it can
        # self-correct (LLM-generated SQL commonly has small, mechanically
        # fixable mistakes like an undeclared table alias).
        prior_error: str | None = None
        last_sql = ""
        for attempt in range(2):
            try:
                sql_query = _call_gemini_for_sql(req.question, schema_context, prior_error)
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=502, detail=f"Could not translate question to SQL: {_model_error_detail(e)}")

            last_sql = sql_query
            if not db.is_read_only_query(sql_query):
                raise HTTPException(
                    status_code=400,
                    detail=f"Generated query was not a read-only SELECT/WITH statement and was rejected: {sql_query}",
                )

            try:
                result = conn.execute(text(sql_query))
                columns = result.keys()
                rows = [dict(zip(columns, row)) for row in result.fetchall()]
                return {"status": "success", "query_executed": sql_query, "data": rows}
            except Exception as e:
                # A failed statement can leave the connection in an aborted
                # transaction; roll it back before the retry can run.
                conn.rollback()
                prior_error = f"SQL: {sql_query}\nError: {e}"

    # Both attempts failed to execute -- surface the last SQL and error.
    raise HTTPException(status_code=400, detail=f"Query execution failed for [{last_sql}]: {prior_error}")


@app.get("/api/download/all")
def download_all():
    """Returns a ZIP archive containing one CSV per table currently stored
    in the database (i.e. reflecting any /api/refine edits, not just the
    original generation)."""
    if not _SCHEMA_CACHE:
        raise HTTPException(status_code=404, detail="No data has been generated yet.")

    tables = {name: db.fetch_table_rows(engine, name) for name in _SCHEMA_CACHE}
    zip_bytes = db.build_zip_of_all_tables(tables)
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=synthetic_data.zip"},
    )
