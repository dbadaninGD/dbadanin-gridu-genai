# Data Assistant — Frontend

A Streamlit UI for the Synthetic Data Engine backend: upload a DDL schema, generate synthetic data, preview and refine it table by table, download it, and ask questions about it in plain English.

## Architecture

`app.py` is a stateless Streamlit app with two tabs (`st.sidebar.radio`):

- **Data Generation** — upload a schema file, set a prompt and generation parameters, hit **Generate**. The response from `POST /api/generate` (a `{table_name: [row, ...]}` dict) is kept in `st.session_state["generated_data"]` for the rest of the browser session. Each table can be previewed, downloaded individually as CSV, or edited with a free-text instruction sent to `POST /api/refine`. A **Download All (ZIP)** button bundles every table's current preview into a single archive client-side.
- **Talk to your data** — a chat interface (`st.chat_message`/`st.chat_input`) that sends each question to `POST /api/query` and renders the returned rows plus the SQL that produced them.

Everything that doesn't need a running Streamlit session — building request payloads, building the ZIP archive, decoding the uploaded file — lives in `utils.py` instead of `app.py`, specifically so it can be unit tested (see below) without needing `streamlit run`.

## How to use it

### Via Docker Compose (recommended)

From the repo root:

```bash
cp .env.example .env
docker compose up -d --build
```

Then open `http://localhost:8501`.

### Standalone (against a backend running elsewhere)

```bash
cd frontend
pipenv install --dev
export BACKEND_URL=http://localhost:8000   # defaults to http://backend:8000, the docker-compose service name
pipenv run streamlit run app.py
```

### Walkthrough

1. **Data Generation** tab: click **Upload DDL Schema** and pick a `.sql`/`.txt`/`.ddl` file containing one or more `CREATE TABLE` statements (see `backend/tests/fixtures/*.ddl` for real examples — a library system, a company/employee schema, a restaurant schema).
2. Optionally type extra instructions in the **Prompt** box, e.g. *"skew order dates toward the last 30 days"*.
3. Adjust **Temperature** (creativity of the generated values), **Max Tokens** (per-request output cap), and **Rows per table** (how many rows each table gets — up to 1000; large requests are automatically batched by the backend).
4. Click **Generate**. Once it finishes, use the table selector to preview each table, **Download CSV** for just that table, or **Download All (ZIP)** for everything.
5. To adjust a table without regenerating it, type an instruction in **Enter quick edit instructions...** (e.g. *"change all Status values to Active"*) and click **Submit** — this calls `/api/refine`, which protects primary/foreign key columns from being changed and persists the result back to Postgres.
6. Switch to the **Talk to your data** tab and ask a question in plain English, e.g. *"Which author has written the most books?"*.

## Running the tests

```bash
cd frontend
pipenv install --dev
pipenv run pytest tests.py -v
```

These test the pure functions in `utils.py` (payload shapes, CSV/ZIP export, upload validation) directly with real `pandas` — they deliberately never import `app.py`, since that module only runs correctly inside `streamlit run`.

## Limitations

- **Session volatility.** Generated-but-not-downloaded data lives in `st.session_state`; reloading the browser tab loses it (though it's still sitting in Postgres — re-running `/api/query` against it in the "Talk to your data" tab still works, it just won't repopulate the Data Generation preview).
- **No pagination.** `st.dataframe` renders the full table client-side; a few thousand rows is fine, but the UI wasn't built to page through very large previews.
- **Single browser session assumption.** Like the backend, there's no concept of separate users — two browser tabs generating different schemas against the same backend will see each other's data once both hit "Generate" (Postgres has one schema at a time, matching whatever was generated last).
- **The upload only accepts DDL, not natural-language schema descriptions.** The `Supported formats: SQL, JSON` label on the original mockup implied a JSON schema format could also be accepted; that path was never implemented on the backend, so only `.sql`/`.txt`/`.ddl` files containing actual `CREATE TABLE` statements work.
