# Synthetic Data Engine

A small conversational AI app: upload a DDL schema, generate constraint-aware synthetic data for it with Gemini (via Vertex AI), preview/refine/download it, and ask questions about it in plain English.

- **`backend/`** — FastAPI service (schema parsing, generation, Postgres). See `backend/README.md` for architecture, API examples, and how to run its tests.
- **`frontend/`** — Streamlit UI. See `frontend/README.md` for a walkthrough and how to run its tests.
- **`docker-compose.yml`** (this directory) — runs Postgres + backend + frontend together.
- **`gcp_readme.md`** — step-by-step deployment to a GCP Compute Engine VM.

## Quickstart

```bash
cp .env.example .env
# edit .env: set POSTGRES_PASSWORD and GOOGLE_CLOUD_PROJECT at minimum
gcloud auth application-default login   # so the backend can reach Vertex AI locally

docker compose up -d --build
```

Then open `http://localhost:8501`. The backend API is at `http://localhost:8000` (`/healthz`, `/api/generate`, `/api/refine`, `/api/query`, `/api/download/all`).

To run each service's tests:

```bash
(cd backend && pipenv install --dev && pipenv run pytest -v)
(cd frontend && pipenv install --dev && pipenv run pytest tests.py -v)
```
