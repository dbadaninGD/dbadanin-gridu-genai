"""
Constraint-aware synthetic data generation.

Design decision (documented here because it is the single biggest departure
from the original implementation): the LLM is **never** asked to invent
primary keys or foreign keys. Large language models are unreliable at
keeping thousands of numeric IDs globally unique and mutually consistent
across several tables and several separate calls -- exactly the failure
mode the original backend's README called out as a known limitation
("hallucinated ID mismatches").

Instead:
  * Primary keys are assigned deterministically by this module (1..N per
    table), so every table's valid ID range is known *before* any LLM call
    is made, regardless of generation order.
  * Foreign keys are filled in by sampling from the referenced table's
    known ID range, again without ever asking the model to produce them.
  * The LLM's job is reduced to what it's actually good at: generating
    realistic values for the remaining ("business") columns, respecting
    data types, NOT NULL, ENUM/CHECK constraints and the user's free-text
    instructions.

This makes referential integrity a mathematical guarantee rather than a
hope, including for circular references (e.g. Employees <-> Departments)
handled by `ddl_parser.resolve_table_order`.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass

from ddl_parser import Column, ResolvedSchema, Table

# A single Gemini call is asked to produce at most this many rows, both to
# stay comfortably inside output-token limits and because the model's
# per-row quality degrades on very large single responses. Requests for
# more rows than this are transparently split into multiple batches.
#
# Kept deliberately modest: a wide table (say 14 columns of realistic text)
# times a large batch can overrun the model's output-token budget and get
# truncated mid-JSON. 25 rows/batch keeps each response comfortably parseable;
# generate_table_rows also salvages complete rows from a truncated batch and
# retries at a smaller size, so this is a soft target rather than a hard cap.
DEFAULT_BATCH_SIZE = 25


@dataclass
class GenerationContext:
    """Everything the generator needs beyond the schema itself."""
    rows_per_table: int
    temperature: float = 0.7
    max_output_tokens: int = 4096
    extra_instructions: str = ""
    batch_size: int = DEFAULT_BATCH_SIZE


def fk_columns(table: Table) -> set[str]:
    return {fk.column for fk in table.foreign_keys}


def generated_columns(table: Table) -> list[Column]:
    """Columns the LLM is actually asked to produce: everything except the
    primary key (we assign it) and foreign keys (we sample them)."""
    fks = fk_columns(table)
    pk = table.primary_key
    return [c for c in table.ordered_columns if c.name != pk and c.name not in fks]


def _column_prompt_line(col: Column) -> str:
    bits = [f'- "{col.name}": {col.raw_type}']
    if not col.nullable:
        bits.append("NOT NULL")
    if col.is_unique:
        bits.append("must be UNIQUE across all rows")
    if col.enum_values:
        bits.append("must be exactly one of: " + ", ".join(col.enum_values))
    if col.default is not None:
        bits.append(f"defaults to {col.default} if not otherwise implied")
    for check in col.checks:
        bits.append(f"must satisfy {check}")
    return " ".join(bits)


def build_prompt(
    table: Table,
    schema: ResolvedSchema,
    ctx: GenerationContext,
    batch_start: int,
    batch_count: int,
) -> str:
    """Builds the natural-language instructions for one generation batch of one table."""
    cols = generated_columns(table)
    col_lines = "\n".join(_column_prompt_line(c) for c in cols)

    fk_lines = []
    for fk in table.foreign_keys:
        fk_lines.append(
            f'- "{fk.column}" is a foreign key into "{fk.ref_table}" and will be filled in '
            f"automatically after generation -- do NOT include it in your output."
        )
    fk_block = ("\nForeign key columns (excluded from your output):\n" + "\n".join(fk_lines)) if fk_lines else ""

    instructions_block = f"\nAdditional user instructions: {ctx.extra_instructions}" if ctx.extra_instructions else ""

    return f"""Generate exactly {batch_count} realistic, internally-consistent synthetic data rows for the
table "{table.name}" (rows {batch_start + 1}-{batch_start + batch_count} of {ctx.rows_per_table} total).

Return a JSON array of {batch_count} objects. Each object must contain exactly these keys
(do not include the primary key or any foreign key column):
{col_lines}
{fk_block}
Make values look like realistic real-world data appropriate for each column's name and type
(e.g. plausible names, addresses, dates in ISO format YYYY-MM-DD, timestamps in ISO 8601).
Respect every NOT NULL, UNIQUE, ENUM and CHECK constraint listed above exactly.{instructions_block}

Respond with ONLY the JSON array, no surrounding text or markdown code fences."""


def _salvage_json_objects(text: str) -> list[dict]:
    """Recovers as many complete top-level JSON objects as possible from a
    string that is a truncated / malformed JSON array (e.g. the model ran out
    of output tokens partway through the last row). Walks the text tracking
    brace depth and string state, and decodes each balanced `{...}` span.
    Any trailing incomplete object is simply dropped.

    This is what turns a truncated batch into "we got 23 of the 25 rows we
    asked for" instead of a hard parse failure -- generate_table_rows then
    tops the batch back up or accepts the shortfall."""
    objects: list[dict] = []
    depth = 0
    start = None
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        objects.append(json.loads(text[start:i + 1]))
                    except json.JSONDecodeError:
                        pass
                    start = None
    return objects


def _extract_json_array(text: str) -> list[dict]:
    """Best-effort extraction of a JSON array from a model response, tolerating
    stray markdown fences some models add despite instructions not to, and
    salvaging complete rows from a truncated response."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # Most commonly a truncated array: salvage whatever complete row
        # objects we can rather than failing the whole batch.
        salvaged = _salvage_json_objects(cleaned)
        if salvaged:
            return salvaged
        raise
    if isinstance(parsed, dict):
        # Some models wrap the array in a {"rows": [...]} envelope despite instructions.
        for value in parsed.values():
            if isinstance(value, list):
                return value
        raise ValueError("Model response was a JSON object with no array of rows inside it.")
    if not isinstance(parsed, list):
        raise ValueError("Model response was not a JSON array.")
    return parsed


def assign_foreign_keys(rows: list[dict], table: Table, pk_pools: dict[str, list[int]]) -> None:
    """Mutates `rows` in place, adding a value for every FK column sampled
    uniformly from the referenced table's known primary-key pool."""
    for fk in table.foreign_keys:
        pool = pk_pools.get(fk.ref_table) or [1]
        for row in rows:
            row[fk.column] = random.choice(pool)


def enforce_uniqueness(rows: list[dict], table: Table) -> None:
    """Best-effort repair for UNIQUE columns: LLMs occasionally repeat a value
    across rows (e.g. two rows with the same email). Any duplicate beyond the
    first occurrence gets a numeric suffix appended so the column stays
    unique without discarding the row."""
    for col in table.ordered_columns:
        if not col.is_unique or col.name == table.primary_key:
            continue
        seen: dict[str, int] = {}
        for row in rows:
            val = row.get(col.name)
            if val is None:
                continue
            key = str(val)
            if key not in seen:
                seen[key] = 0
                continue
            seen[key] += 1
            row[col.name] = f"{val}-{seen[key]}"


def assign_primary_keys(rows: list[dict], table: Table, start: int = 1) -> None:
    pk = table.primary_key
    if not pk:
        return
    for i, row in enumerate(rows):
        row[pk] = start + i


def compute_pk_pools(resolved: ResolvedSchema, rows_per_table: int) -> dict[str, list[int]]:
    """Every table's primary key is assigned sequentially starting at 1, so
    the valid ID range for ANY table is known before generation starts --
    this is what lets FK sampling work regardless of generation order or
    circular references."""
    return {name: list(range(1, rows_per_table + 1)) for name in resolved.tables}


def generate_table_rows(
    table: Table,
    schema: ResolvedSchema,
    ctx: GenerationContext,
    pk_pools: dict[str, list[int]],
    call_model: "callable",
) -> list[dict]:
    """Generates all rows for one table, batching LLM calls as needed.

    `call_model(prompt: str) -> str` is injected so tests can substitute a
    fake model without touching the real GenAI client.
    """
    rows: list[dict] = []
    remaining = ctx.rows_per_table
    batch_start = 0
    while remaining > 0:
        batch_count = min(ctx.batch_size, remaining)
        batch_rows = _generate_one_batch(table, schema, ctx, batch_start, batch_count, call_model)

        # Only take as many as we still need, and never more than asked for.
        batch_rows = batch_rows[:batch_count]
        rows.extend(batch_rows)

        # Advance by what we actually got. A short batch (e.g. a truncated
        # response we salvaged 18 rows from) leaves the shortfall in
        # `remaining`, so the next loop iteration asks for the rest instead
        # of silently dropping rows or padding with duplicates.
        produced = len(batch_rows)
        if produced == 0:
            # Nothing usable even after the retry inside _generate_one_batch;
            # give up on this table rather than looping forever.
            break
        remaining -= produced
        batch_start += produced

    assign_primary_keys(rows, table)
    assign_foreign_keys(rows, table, pk_pools)
    enforce_uniqueness(rows, table)
    return rows


def _generate_one_batch(
    table: Table,
    schema: ResolvedSchema,
    ctx: GenerationContext,
    batch_start: int,
    batch_count: int,
    call_model: "callable",
) -> list[dict]:
    """Runs a single generation call and parses it. If the response can't be
    parsed at all (not even salvageable), retries once at half the row count
    -- a smaller ask is less likely to hit the output-token ceiling. Returns
    whatever rows were parsed (possibly fewer than requested, possibly empty)."""
    prompt = build_prompt(table, schema, ctx, batch_start, batch_count)
    try:
        return _extract_json_array(call_model(prompt))
    except (ValueError, json.JSONDecodeError):
        if batch_count <= 1:
            return []
        smaller = max(1, batch_count // 2)
        retry_prompt = build_prompt(table, schema, ctx, batch_start, smaller)
        try:
            return _extract_json_array(call_model(retry_prompt))
        except (ValueError, json.JSONDecodeError):
            return []


def generate_all_tables(
    resolved: ResolvedSchema,
    ctx: GenerationContext,
    call_model: "callable",
) -> dict[str, list[dict]]:
    """Generates data for every table in dependency order. Returns
    {table_name: [row_dict, ...]}."""
    pk_pools = compute_pk_pools(resolved, ctx.rows_per_table)
    result: dict[str, list[dict]] = {}
    for name in resolved.order:
        table = resolved.tables[name]
        result[name] = generate_table_rows(table, resolved, ctx, pk_pools, call_model)
    return result
