"""
DDL parsing for the Synthetic Data Engine.

This module turns a raw CREATE TABLE / ALTER TABLE script (MySQL-ish DDL is
what users tend to paste in, e.g. `AUTO_INCREMENT`, `ENUM(...)`) into a
small, structured model (`Table`, `Column`, `ForeignKey`) that the rest of
the backend uses to:

  1. create an equivalent schema in Postgres (see `to_postgres_ddl`), and
  2. drive constraint-aware synthetic data generation (see `generator.py`).

The parser is intentionally regex/bracket-based rather than a full SQL
grammar: schema-generation prompts in practice use a small, predictable
subset of DDL (CREATE TABLE with column defs + inline/ALTER FOREIGN KEYs),
and a full SQL parser would be overkill for a training project. It is
tolerant of the things real-world "pasted" schemas commonly contain:
trailing line comments, ENUM value lists, composite/table-level PRIMARY
KEY and FOREIGN KEY clauses, and FOREIGN KEYs added later via
`ALTER TABLE ... ADD CONSTRAINT`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


class DDLParseError(ValueError):
    """Raised when the input does not contain any parseable CREATE TABLE statements."""


@dataclass
class ForeignKey:
    column: str
    ref_table: str
    ref_column: str


@dataclass
class Column:
    name: str
    raw_type: str  # e.g. "VARCHAR(255)", "ENUM('A','B')"
    nullable: bool = True
    is_primary_key: bool = False
    is_unique: bool = False
    auto_increment: bool = False
    default: str | None = None
    enum_values: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)

    @property
    def is_enum(self) -> bool:
        return bool(self.enum_values)

    @property
    def base_type(self) -> str:
        """Type keyword without its (args), e.g. 'VARCHAR(255)' -> 'VARCHAR'."""
        return re.match(r"[A-Za-z_]+", self.raw_type).group(0).upper()


@dataclass
class Table:
    name: str
    columns: dict[str, Column] = field(default_factory=dict)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    table_checks: list[str] = field(default_factory=list)

    @property
    def primary_key(self) -> str | None:
        """Returns the single-column primary key name, if any (composite PKs are unsupported)."""
        for col in self.columns.values():
            if col.is_primary_key:
                return col.name
        return None

    @property
    def ordered_columns(self) -> list[Column]:
        return list(self.columns.values())


# ---------------------------------------------------------------------------
# Tokenizing helpers
# ---------------------------------------------------------------------------

def _strip_line_comments(ddl: str) -> str:
    """Removes `-- ...` line comments. Good enough since none of our column
    definitions legitimately contain a literal `--` inside a string value."""
    return re.sub(r"--[^\n]*", "", ddl)


def _find_balanced(text: str, open_pos: int) -> int:
    """Given the index of an opening '(' in `text`, returns the index of its
    matching closing ')'."""
    depth = 0
    for i in range(open_pos, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    raise DDLParseError("Unbalanced parentheses in DDL near position %d" % open_pos)


def _split_top_level(body: str) -> list[str]:
    """Splits a CREATE TABLE body on commas that are not nested inside parens."""
    parts = []
    depth = 0
    current = []
    for ch in body:
        if ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        tail = "".join(current).strip()
        if tail:
            parts.append(tail)
    return [p for p in parts if p]


_IDENT = r"[`\"\[]?(\w+)[`\"\]]?"


# ---------------------------------------------------------------------------
# Column / constraint parsing
# ---------------------------------------------------------------------------

def _parse_enum_values(args: str) -> list[str]:
    return [v.strip().strip("'\"") for v in re.findall(r"'([^']*)'|\"([^\"]*)\"", args)
            for v in v if v] or [v.strip("'\" ") for v in args.split(",")]


def _parse_column_def(part: str) -> Column | None:
    m = re.match(r"^" + _IDENT + r"\s+(.+)$", part, re.DOTALL)
    if not m:
        return None
    name, rest = m.group(1), m.group(2).strip()

    type_match = re.match(r"([A-Za-z_]+)\s*(\(([^()]*)\))?", rest)
    if not type_match:
        return None
    type_name = type_match.group(1)
    type_args = type_match.group(3)
    raw_type = type_name.upper() + (f"({type_args})" if type_args is not None else "")
    remainder = rest[type_match.end():]

    col = Column(name=name, raw_type=raw_type)

    if type_name.upper() == "ENUM" and type_args:
        col.enum_values = [v for v in re.findall(r"'([^']*)'", type_args)]

    if re.search(r"(?i)\bPRIMARY\s+KEY\b", remainder):
        col.is_primary_key = True
        col.nullable = False
    if re.search(r"(?i)\bNOT\s+NULL\b", remainder):
        col.nullable = False
    elif re.search(r"(?i)(?<!NOT\s)\bNULL\b", remainder):
        col.nullable = True
    if re.search(r"(?i)\bAUTO_INCREMENT\b", remainder):
        col.auto_increment = True
    if re.search(r"(?i)\bUNIQUE\b", remainder):
        col.is_unique = True

    default_match = re.search(r"(?i)\bDEFAULT\s+('(?:[^']*)'|\"[^\"]*\"|[^\s,]+)", remainder)
    if default_match:
        col.default = default_match.group(1)

    check_match = re.search(r"(?i)\bCHECK\s*(\([^)]*\))", remainder)
    if check_match:
        col.checks.append(check_match.group(1))

    ref_match = re.search(
        r"(?i)\bREFERENCES\s+" + _IDENT + r"\s*\(\s*" + _IDENT + r"\s*\)", remainder
    )
    inline_fk = None
    if ref_match:
        inline_fk = ForeignKey(column=name, ref_table=ref_match.group(1), ref_column=ref_match.group(2))

    return col, inline_fk


def _parse_table_level_fk(part: str) -> ForeignKey | None:
    m = re.search(
        r"(?i)FOREIGN\s+KEY\s*\(\s*" + _IDENT + r"\s*\)\s*REFERENCES\s+" + _IDENT + r"\s*\(\s*" + _IDENT + r"\s*\)",
        part,
    )
    if not m:
        return None
    return ForeignKey(column=m.group(1), ref_table=m.group(2), ref_column=m.group(3))


def _parse_table_level_pk(part: str) -> list[str]:
    m = re.search(r"(?i)PRIMARY\s+KEY\s*\(([^)]+)\)", part)
    if not m:
        return []
    return [c.strip().strip("`\"") for c in m.group(1).split(",")]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_ddl(ddl_text: str) -> dict[str, Table]:
    """Parses a DDL script into an ordered dict of table name -> Table.

    Table order in the returned dict matches the order tables were declared
    in the source text (NOT dependency order -- see `resolve_table_order`
    for that).
    """
    cleaned = _strip_line_comments(ddl_text)
    tables: dict[str, Table] = {}

    for m in re.finditer(r"(?i)CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?" + _IDENT + r"\s*\(", cleaned):
        table_name = m.group(1)
        open_paren = m.end() - 1
        close_paren = _find_balanced(cleaned, open_paren)
        body = cleaned[open_paren + 1:close_paren]

        table = Table(name=table_name)
        for part in _split_top_level(body):
            stripped = part.strip()
            if not stripped:
                continue
            upper = stripped.upper()
            if upper.startswith("PRIMARY KEY"):
                for col_name in _parse_table_level_pk(stripped):
                    if col_name in table.columns:
                        table.columns[col_name].is_primary_key = True
                        table.columns[col_name].nullable = False
                continue
            if upper.startswith("FOREIGN KEY") or upper.startswith("CONSTRAINT"):
                fk = _parse_table_level_fk(stripped)
                if fk:
                    table.foreign_keys.append(fk)
                pk_cols = _parse_table_level_pk(stripped)
                for col_name in pk_cols:
                    if col_name in table.columns:
                        table.columns[col_name].is_primary_key = True
                        table.columns[col_name].nullable = False
                continue
            if upper.startswith("UNIQUE"):
                inner = re.search(r"\(([^)]+)\)", stripped)
                if inner:
                    for col_name in [c.strip().strip("`\"") for c in inner.group(1).split(",")]:
                        if col_name in table.columns:
                            table.columns[col_name].is_unique = True
                continue
            if upper.startswith("CHECK"):
                table.table_checks.append(stripped)
                continue
            if upper.startswith("KEY") or upper.startswith("INDEX"):
                continue  # plain (non-unique) indexes don't affect generation

            parsed = _parse_column_def(stripped)
            if not parsed:
                continue
            col, inline_fk = parsed
            table.columns[col.name] = col
            if inline_fk:
                table.foreign_keys.append(inline_fk)

        tables[table_name] = table

    if not tables:
        raise DDLParseError("No CREATE TABLE statements found in the provided schema.")

    # Pick up FOREIGN KEYs added via ALTER TABLE ... ADD CONSTRAINT ... (a common
    # pattern for breaking circular references, e.g. two tables that both
    # reference each other).
    for m in re.finditer(
        r"(?i)ALTER\s+TABLE\s+" + _IDENT + r"\s+ADD\s+(?:CONSTRAINT\s+\w+\s+)?"
        r"FOREIGN\s+KEY\s*\(\s*" + _IDENT + r"\s*\)\s*REFERENCES\s+" + _IDENT + r"\s*\(\s*" + _IDENT + r"\s*\)",
        cleaned,
    ):
        table_name, col, ref_table, ref_col = m.groups()
        if table_name in tables:
            tables[table_name].foreign_keys.append(
                ForeignKey(column=col, ref_table=ref_table, ref_column=ref_col)
            )

    return tables


@dataclass
class ResolvedSchema:
    tables: dict[str, Table]
    order: list[str]                # topological table order (dependencies first)
    deferred_fks: list[tuple[str, ForeignKey]]  # (table_name, fk) pairs whose FK
                                                 # constraint must be added after
                                                 # all tables exist (cycles + self-refs)


def _strongly_connected_components(nodes: list[str], edges: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan's SCC algorithm, iterating nodes/neighbors in sorted order so the
    result is deterministic regardless of Python's hash-randomized set order."""
    index_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    sccs: list[list[str]] = []

    def strongconnect(v: str):
        indices[v] = lowlink[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack.add(v)

        for w in sorted(edges.get(v, ())):
            if w not in indices:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], indices[w])

        if lowlink[v] == indices[v]:
            component = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                component.append(w)
                if w == v:
                    break
            sccs.append(sorted(component))

    for node in sorted(nodes):
        if node not in indices:
            strongconnect(node)

    return sccs


def resolve_table_order(tables: dict[str, Table]) -> ResolvedSchema:
    """Computes a dependency-respecting table creation order, deferring any
    FOREIGN KEY that would otherwise create a cycle (including a table that
    references itself). Deferred FKs are added via `ALTER TABLE` after every
    table has been created, which Postgres allows regardless of ordering.

    FKs are only ever deferred when they are genuinely part of a cycle
    (found via Tarjan's strongly-connected-components algorithm) -- a table
    that is merely *transitively* blocked by a cycle elsewhere (e.g. it
    depends on a table that is itself stuck in an unrelated cycle) is left
    with all of its FKs inline once that cycle is resolved.
    """
    deferred: list[tuple[str, ForeignKey]] = []
    edges: dict[str, set[str]] = {}

    for name, table in tables.items():
        deps = set()
        for fk in table.foreign_keys:
            if fk.ref_table not in tables:
                continue  # dangling reference to a table not in this schema; ignore
            if fk.ref_table == name:
                deferred.append((name, fk))  # self-reference: always deferred
                continue
            deps.add(fk.ref_table)
        edges[name] = deps

    # Break every cycle up front by removing one edge per strongly-connected
    # component until each component is a singleton. Recomputing SCCs after
    # each removal keeps this correct for nested/overlapping cycles.
    while True:
        sccs = [c for c in _strongly_connected_components(list(tables.keys()), edges) if len(c) > 1]
        if not sccs:
            break
        component = sorted(sccs)[0]
        comp_set = set(component)
        # Defer one edge from the component member with the fewest in-component
        # outgoing edges (deterministic tie-break: alphabetically first).
        t = sorted(component, key=lambda n: (len(edges[n] & comp_set), n))[0]
        target = sorted(edges[t] & comp_set)[0]
        edges[t].discard(target)
        for fk in tables[t].foreign_keys:
            if fk.ref_table == target:
                deferred.append((t, fk))

    # The graph is now acyclic -- a plain Kahn's algorithm produces the order.
    remaining = set(tables.keys())
    order: list[str] = []
    while remaining:
        ready = sorted(t for t in remaining if not (edges[t] & remaining))
        order.extend(ready)
        remaining -= set(ready)

    return ResolvedSchema(tables=tables, order=order, deferred_fks=deferred)


# ---------------------------------------------------------------------------
# Postgres DDL emission
# ---------------------------------------------------------------------------

_TYPE_MAP = {
    "INT": "INTEGER",
    "INTEGER": "INTEGER",
    "SMALLINT": "SMALLINT",
    "BIGINT": "BIGINT",
    "TINYINT": "SMALLINT",
    "DATETIME": "TIMESTAMP",
    "TIMESTAMP": "TIMESTAMP",
    "FLOAT": "DOUBLE PRECISION",
    "DOUBLE": "DOUBLE PRECISION",
    "BOOL": "BOOLEAN",
    "BOOLEAN": "BOOLEAN",
}


def _postgres_column_type(col: Column) -> str:
    if col.is_enum:
        return "VARCHAR(255)"
    base = col.base_type
    args_match = re.match(r"[A-Za-z_]+\((.*)\)", col.raw_type)
    args = args_match.group(1) if args_match else None
    mapped = _TYPE_MAP.get(base, base)
    if base in ("VARCHAR", "CHAR", "DECIMAL", "NUMERIC") and args:
        return f"{mapped}({args})"
    return mapped


def _quote_default(col: Column) -> str | None:
    if col.default is None:
        return None
    val = col.default
    if val.upper() in ("CURRENT_TIMESTAMP", "NOW()", "TRUE", "FALSE", "NULL"):
        return val.upper() if val.upper() != "NOW()" else "CURRENT_TIMESTAMP"
    if val.startswith("'") or val.startswith('"'):
        return "'" + val.strip("'\"") + "'"
    try:
        float(val)
        return val
    except ValueError:
        return "'" + val + "'"


def to_postgres_ddl(resolved: ResolvedSchema) -> list[str]:
    """Emits CREATE TABLE statements (in dependency order, with non-deferred
    FOREIGN KEYs inline) followed by ALTER TABLE statements for the deferred
    ones. Every statement is idempotent (DROP ... IF EXISTS CASCADE first)
    so re-generating overwrites any previous run's schema."""
    statements: list[str] = ['SET client_min_messages TO WARNING;']

    for name in reversed(resolved.order):
        statements.append(f'DROP TABLE IF EXISTS "{name}" CASCADE;')

    deferred_cols = {(t, fk.column) for t, fk in resolved.deferred_fks}

    for name in resolved.order:
        table = resolved.tables[name]
        lines = []
        for col in table.ordered_columns:
            parts = [f'"{col.name}"', _postgres_column_type(col)]
            if col.is_primary_key:
                parts.append("PRIMARY KEY")
            else:
                parts.append("NOT NULL" if not col.nullable else "NULL")
            if col.is_unique and not col.is_primary_key:
                parts.append("UNIQUE")
            default = _quote_default(col)
            if default is not None:
                parts.append(f"DEFAULT {default}")
            if col.is_enum:
                values = ", ".join(f"'{v}'" for v in col.enum_values)
                parts.append(f'CHECK ("{col.name}" IN ({values}))')
            lines.append("    " + " ".join(parts))

        for fk in table.foreign_keys:
            if (name, fk.column) in deferred_cols:
                continue
            lines.append(
                f'    FOREIGN KEY ("{fk.column}") REFERENCES "{fk.ref_table}"("{fk.ref_column}")'
            )

        statements.append(f'CREATE TABLE "{name}" (\n' + ",\n".join(lines) + "\n);")

    for i, (table_name, fk) in enumerate(resolved.deferred_fks):
        statements.append(
            f'ALTER TABLE "{table_name}" ADD CONSTRAINT "fk_{table_name}_{fk.column}_{i}" '
            f'FOREIGN KEY ("{fk.column}") REFERENCES "{fk.ref_table}"("{fk.ref_column}") '
            f"DEFERRABLE INITIALLY DEFERRED;"
        )

    return statements
