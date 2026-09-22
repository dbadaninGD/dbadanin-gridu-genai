# Synthetic Data Engine — Backend

A FastAPI service that turns a pasted DDL schema into a live Postgres database full of realistic, referentially-correct synthetic data, using Gemini (via Vertex AI) to write the actual column values.

## Architecture

The backend is split into four small modules instead of one big `main.py`, because the interesting logic — schema parsing and constraint-aware generation — is exactly the part that needs to be unit-testable without a live database or a real GenAI call.

| Module | Responsibility |
| --- | --- |
| `ddl_parser.py` | Parses CREATE TABLE / ALTER TABLE DDL into a `Table`/`Column`/`ForeignKey` model, computes a dependency-respecting table creation order (breaking cycles when needed), and emits equivalent Postgres DDL. |
| `generator.py` | Drives constraint-aware generation: builds the prompt for each table, calls the model in batches, and assigns primary/foreign keys itself. |
| `db.py` | All database access — schema creation, parameterized inserts/updates, the NL-to-SQL read-only guard, ZIP export. |
| `main.py` | Thin FastAPI orchestration layer over the three modules above. |

### The key design decision: the LLM never invents IDs

The original version of this backend asked Gemini to generate primary keys and foreign keys directly, then inserted whatever it returned. That's unreliable at any real scale — LLMs are not good at keeping thousands of numeric IDs globally unique and mutually consistent across several tables and several separate calls, which is exactly the "hallucinated ID mismatches" problem previously listed here as a known limitation.

This version doesn't ask it to. Instead:

* Every table's primary key is assigned **deterministically**, 1..N, by this backend — not the model. Because `rows_per_table` (N) is the same for every table and is known before generation starts, every table's *valid ID range* is known up front, before any Gemini call is made, regardless of what order tables are generated in.
* Foreign keys are filled in by sampling from the referenced table's known ID range — also without asking the model.
* Gemini's job is reduced to what it's actually good at: writing realistic values for the remaining "business" columns (names, addresses, dates, free text), respecting each column's type, `NOT NULL`, `ENUM`/`CHECK` constraints, and your free-text instructions.

This makes referential integrity a guarantee rather than a hope — including for **circular references** (e.g. an `Employees` table with a `department_id` FK and a `Departments` table with a `manager_id` FK back to `Employees`), which `ddl_parser.resolve_table_order` detects and breaks by declaring the offending foreign key as `DEFERRABLE INITIALLY DEFERRED` and adding it via `ALTER TABLE` after every table exists. Every insert for a single `/api/generate` call runs inside one transaction, so even a `NOT NULL` circular foreign key resolves cleanly — Postgres only checks deferred constraints at `COMMIT`, by which point every table already has its full set of rows.

`UNIQUE` columns get a light best-effort repair pass (a numeric suffix on any duplicate Gemini happens to produce) before insert, as a second line of defense on top of Postgres' own constraint.

### Handling large row counts (and truncation)

A single Gemini call is capped at 25 rows (see `generator.DEFAULT_BATCH_SIZE`) to stay well inside output-token limits and keep per-row quality high. Asking for more than that (up to the API's cap of 1000 rows/table) transparently issues multiple calls per table — you don't need to do anything differently for 1000 rows than for 10.

Two things keep a batch from failing the whole request when the model's output is imperfect:

- **Generation gets its own token budget.** The UI "Max Tokens" field is only a hint/floor — each generation batch runs with at least `GENERATION_MIN_OUTPUT_TOKENS` (default 8192) so a wide table's rows don't get truncated mid-JSON. (Wiring that slider directly to the per-batch output limit, as the first version did, is what produced spurious 502s on wide schemas.)
- **The parser salvages what it can.** If a response is still truncated, `_extract_json_array` recovers every complete row object from the partial JSON instead of throwing, and `generate_table_rows` tops the batch back up on the next call. A batch that can't be parsed at all is retried once at half size, and only then given up on — the table simply ends up with fewer rows rather than the request returning an error.

### Observability

Every model call (`generate_table_batch`, `refine_table_data`, `nl_to_sql_query`) is wrapped in a Langfuse `@observe` span, so latency, token usage, and input/output payloads for each one show up in your Langfuse project. Tracing is **only** enabled when both `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are set — otherwise `@observe` is a pure pass-through and the SDK is never even imported, so a keyless deploy produces no tracing and none of the "Failed to export span batch code: 401" log noise the SDK would otherwise emit.

### Error reporting

GenAI/Vertex failures (auth, quota, model availability) and query-execution errors are caught and returned as the API response `detail`, and the Streamlit UI displays that message. So a misconfigured project shows up in the browser as e.g. "Data generation failed: 403: Permission denied on ... gemini-2.5-pro" rather than a bare 500 you'd have to read the container logs to diagnose.

`/api/query` also **self-corrects once**: LLM-generated SQL frequently has small, mechanical mistakes (an undeclared table alias, a missing quote). If the first query fails to execute, the endpoint hands the model its own SQL plus the PostgreSQL error and runs the corrected version; only if that also fails does it return a 400 (with both the SQL and the error in the detail).

## Endpoints

| Method & path | Purpose |
| --- | --- |
| `POST /api/generate` | Parse a DDL schema, (re)create it in Postgres, generate `rows_per_table` rows per table, store them. |
| `POST /api/refine` | Apply a free-text edit to one table's rows; persists the result back to Postgres. |
| `POST /api/query` | Natural-language question → read-only SQL → rows, against whatever was last generated. |
| `GET /api/download/all` | A ZIP with one CSV per table, read live from Postgres (reflects any `/api/refine` edits). |
| `GET /healthz` | Liveness check (used by `docker-compose.yml` and the GCP deployment). |

## How to use it

### Locally, without Docker

```bash
cd backend
pipenv install --dev
gcloud auth application-default login   # so the GenAI client can reach Vertex AI
export GOOGLE_CLOUD_PROJECT=your-gcp-project-id
export DATABASE_URL=postgresql://postgres:password@localhost:5432/datagen_db
pipenv run uvicorn main:app --reload
```

### Via Docker Compose

From the repo root (not from inside `backend/` — see the root `README.md`):

```bash
cp .env.example .env   # fill in your GCP project id, DB password, etc.
docker compose up -d --build
```

### Example requests

Generate 200 rows per table from an uploaded schema:

```bash
curl -X POST http://localhost:8000/api/generate \
  -H "Content-Type: application/json" \
  -d '{
        "ddl_schema": "CREATE TABLE Authors (author_id INT PRIMARY KEY AUTO_INCREMENT, first_name VARCHAR(100) NOT NULL, last_name VARCHAR(100) NOT NULL); CREATE TABLE Books (book_id INT PRIMARY KEY AUTO_INCREMENT, title VARCHAR(255) NOT NULL, author_id INT NOT NULL, FOREIGN KEY (author_id) REFERENCES Authors(author_id));",
        "prompt": "Make the book titles sound like real novels",
        "temperature": 0.8,
        "rows_per_table": 200
      }'
```

Apply a quick edit to one table's preview:

```bash
curl -X POST http://localhost:8000/api/refine \
  -H "Content-Type: application/json" \
  -d '{
        "table_name": "Authors",
        "instructions": "Make all last names start with a different letter than the first name",
        "current_data": [{"author_id": 1, "first_name": "Jane", "last_name": "Doe"}]
      }'
```

Ask a question about the generated data:

```bash
curl -X POST http://localhost:8000/api/query \
  -H "Content-Type: application/json" \
  -d '{"question": "How many books does each author have?"}'
```

Download everything as a ZIP:

```bash
curl -o synthetic_data.zip http://localhost:8000/api/download/all
```

## Dependencies

`Pipfile.lock` in this repo was generated (with a slightly different `Pipfile` — it was missing an explicit `uvicorn` entry that the lock still resolved as a transitive pin) before this rewrite. Because of that drift, `Dockerfile` intentionally installs with `pipenv install --system --ignore-pipfile` rather than the stricter `pipenv install --deploy` (which fails on any Pipfile/lock hash mismatch). If you want that stricter check back, run `pipenv lock` locally after any `Pipfile` change and commit the regenerated `Pipfile.lock`.

Notably, `Pipfile.lock` resolves `langfuse==4.x`, whose `@observe` decorator moved from `langfuse.decorators.observe` (v2) to the top-level `langfuse.observe` (v3+) — `main.py` uses the current import (`from langfuse import observe`).

## Running the tests

```bash
cd backend
pipenv install --dev
pipenv run pytest -v
```

`tests/test_ddl_parser.py` and `tests/test_generator.py` run against three real-world sample schemas checked into `tests/fixtures/` (a 9-table library system with genuine circular foreign keys, a 7-table company/employee schema with a self-referencing FK, and a 7-table restaurant schema) — the same ones used during development to validate the generated DDL against a real Postgres server.

## Limitations

- **DDL dialect coverage.** The parser is regex/bracket-based, not a full SQL grammar. It comfortably handles the common subset real schemas use (inline and table-level `PRIMARY KEY`/`FOREIGN KEY`, `ALTER TABLE ... ADD CONSTRAINT`, `ENUM(...)`, `CHECK(...)`, `AUTO_INCREMENT`), but an unusual dialect construct (e.g. Postgres-specific `GENERATED ALWAYS AS IDENTITY`, multi-column composite foreign keys, deferred index definitions) may not parse as intended. Composite primary keys are not supported — only the first `PRIMARY KEY` column found is used.
- **Single active schema.** The backend caches only the most recently generated schema (`_SCHEMA_CACHE`) to know each table's key columns for `/api/refine` and `/api/download/all`. There's no multi-tenancy or session concept — this mirrors the rest of the app (no auth anywhere), but means two people generating different schemas against the same backend instance will clobber each other.
- **`/api/query`'s safety guard is a prefix check**, not a full SQL sanitizer: it rejects anything that doesn't start with `SELECT`/`WITH`, which blocks the obvious cases (`DROP`, `DELETE`, `UPDATE`) but wouldn't catch, say, a read query that calls a mutating stored function. Don't point this at a database that has anything you care about beyond the synthetic data it generated.
- **Business-column realism is still bounded by the model.** Generated names/addresses/etc. can repeat across rows or batches for a given table — `UNIQUE` columns get an automatic disambiguation pass, but non-unique columns can still look repetitive at high row counts, especially with low `temperature`.
- **No authentication or rate limiting** on any endpoint — fine for a training/demo deployment behind a firewall rule restricted to your own IP (see `gcp_readme.md`), not something to expose publicly as-is.
