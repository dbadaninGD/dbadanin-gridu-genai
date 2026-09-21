# Synthetic Data Engine - Backend

## Architecture
Built with FastAPI, this service acts as the orchestration layer. It accepts DDL structures and prompts from the UI, formats them as context, and calls the Google Gemini 2.5 Pro model via Vertex AI using the `google-genai` SDK. The model is constrained to return strictly structured JSON. Responses are then persisted to PostgreSQL. Langfuse decorators track LLM latency, cost, and input/output payloads.

## How to use it
1. Ensure GCP Application Default Credentials are set locally (`gcloud auth application-default login`).
2. Run `docker-compose up backend db`.
3. POST to `/api/generate` with JSON payload containing `prompt` and `ddl_schema`.

## Limitations
- **Token Limits:** Large DDL schemas or requests for massive datasets (e.g., 10,000+ rows in one pass) may exceed the Vertex AI model's context window. Batching mechanisms are required for high-volume generation.
- **Foreign Key Consistency:** While the LLM is instructed to maintain integrity, complex multi-table relational graphs might experience occasional hallucinated ID mismatches.
