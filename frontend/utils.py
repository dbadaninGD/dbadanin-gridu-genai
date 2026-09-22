"""
Pure helper functions used by app.py, pulled out into their own module so
they can be unit tested without a running Streamlit session (Streamlit's
`st.*` calls only work inside an actual app run, so nothing that touches
`st` belongs in here).
"""

from __future__ import annotations

import io
import zipfile

import pandas as pd


def build_generate_payload(
    prompt: str,
    ddl_content: str,
    temperature: float,
    max_tokens: int,
    rows_per_table: int,
) -> dict:
    """Builds the JSON body for POST /api/generate."""
    return {
        "prompt": prompt,
        "ddl_schema": ddl_content,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "rows_per_table": rows_per_table,
    }


def build_refine_payload(table_name: str, instructions: str, current_data: list[dict]) -> dict:
    """Builds the JSON body for POST /api/refine."""
    return {
        "table_name": table_name,
        "instructions": instructions,
        "current_data": current_data,
    }


def dataframe_to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")


def build_zip_of_tables(tables: dict[str, list[dict]]) -> bytes:
    """Builds a ZIP archive with one CSV per table, from the data currently
    held in the Streamlit session (used by the client-side 'Download All'
    button as a fallback when the backend's own /api/download/all isn't
    reachable, and for tests that don't need a live backend)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for table_name, rows in tables.items():
            df = pd.DataFrame(rows)
            zf.writestr(f"{table_name}.csv", dataframe_to_csv_bytes(df))
    return buffer.getvalue()


def is_valid_upload(uploaded_file) -> bool:
    """A DDL file is required; the free-text prompt is optional extra
    instructions, not a required field."""
    return uploaded_file is not None


def extract_error_detail(response, exception) -> str:
    """Pulls the backend's own error message out of a failed request.

    FastAPI returns errors as JSON `{"detail": "..."}`. Without this, the UI
    would only ever show the generic requests exception ("502 Server Error:
    Bad Gateway"), hiding the actual cause (e.g. "Permission denied on
    gemini-2.5-pro"). Falls back to the raw exception string if the body
    isn't the expected JSON shape."""
    if response is not None:
        try:
            detail = response.json().get("detail")
            if detail:
                return detail if isinstance(detail, str) else str(detail)
        except (ValueError, AttributeError):
            pass
    return str(exception)


def decode_uploaded_file(uploaded_file) -> str:
    return uploaded_file.getvalue().decode("utf-8")
