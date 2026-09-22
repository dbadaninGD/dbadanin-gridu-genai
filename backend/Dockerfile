# Synthetic Data Engine -- backend (FastAPI + Vertex AI + Postgres)
FROM python:3.11-slim

# curl is only needed for the HEALTHCHECK below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir pipenv

COPY Pipfile Pipfile.lock ./
# --ignore-pipfile installs the exact versions already resolved in
# Pipfile.lock without re-checking it against Pipfile's hash. We install
# this way (instead of `pipenv install --deploy`) because Pipfile.lock in
# this repo was generated against a slightly different Pipfile; see
# "Dependencies" in README.md. Run `pipenv lock` and commit the result if
# you want the stricter --deploy hash check back.
RUN pipenv install --system --ignore-pipfile

COPY . .

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD curl -f http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
