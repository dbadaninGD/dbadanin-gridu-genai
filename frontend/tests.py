"""
Unit tests for the pure helper functions in utils.py.

These deliberately avoid importing app.py (which imports streamlit and
only runs correctly inside `streamlit run`) -- everything worth unit
testing on the frontend has been extracted into utils.py for exactly this
reason.
"""

import io
import zipfile

import pandas as pd
import pytest

from utils import (
    build_generate_payload,
    build_refine_payload,
    build_zip_of_tables,
    dataframe_to_csv_bytes,
    decode_uploaded_file,
    extract_error_detail,
    is_valid_upload,
)


class _FakeResponse:
    def __init__(self, payload, raises=False):
        self._payload = payload
        self._raises = raises

    def json(self):
        if self._raises:
            raise ValueError("no json body")
        return self._payload


class TestErrorDetail:
    def test_prefers_backend_detail(self):
        resp = _FakeResponse({"detail": "Permission denied on gemini-2.5-pro"})
        assert extract_error_detail(resp, Exception("502 Server Error")) == "Permission denied on gemini-2.5-pro"

    def test_falls_back_to_exception_when_no_response(self):
        assert extract_error_detail(None, Exception("connection refused")) == "connection refused"

    def test_falls_back_when_body_is_not_json(self):
        resp = _FakeResponse(None, raises=True)
        assert extract_error_detail(resp, Exception("502 Bad Gateway")) == "502 Bad Gateway"

    def test_stringifies_non_string_detail(self):
        resp = _FakeResponse({"detail": [{"loc": ["body", "ddl_schema"]}]})
        assert "ddl_schema" in extract_error_detail(resp, Exception("422"))


class TestPayloadBuilding:
    def test_generate_payload_shape(self):
        payload = build_generate_payload(
            prompt="skew toward last month",
            ddl_content="CREATE TABLE x (id INT);",
            temperature=0.9,
            max_tokens=1500,
            rows_per_table=200,
        )
        assert payload == {
            "prompt": "skew toward last month",
            "ddl_schema": "CREATE TABLE x (id INT);",
            "temperature": 0.9,
            "max_tokens": 1500,
            "rows_per_table": 200,
        }

    def test_refine_payload_shape(self):
        payload = build_refine_payload("Orders", "set status to Active", [{"id": 1}])
        assert payload == {
            "table_name": "Orders",
            "instructions": "set status to Active",
            "current_data": [{"id": 1}],
        }


class TestUploadValidation:
    def test_no_file_is_invalid(self):
        assert is_valid_upload(None) is False

    def test_any_uploaded_file_is_valid(self):
        assert is_valid_upload(object()) is True


class TestFileDecoding:
    def test_decodes_utf8_bytes(self):
        class FakeUpload:
            def getvalue(self):
                return "CREATE TABLE x (id INT);".encode("utf-8")

        assert decode_uploaded_file(FakeUpload()) == "CREATE TABLE x (id INT);"


class TestCsvExport:
    def test_dataframe_round_trips_through_csv_bytes(self):
        df = pd.DataFrame([{"id": 1, "name": "Ada"}, {"id": 2, "name": "Bo"}])
        csv_bytes = dataframe_to_csv_bytes(df)
        text = csv_bytes.decode("utf-8")
        assert "id,name" in text
        assert "1,Ada" in text
        assert "2,Bo" in text


class TestZipExport:
    def test_one_csv_per_table(self):
        data = {
            "Authors": [{"author_id": 1, "first_name": "Jane"}],
            "Books": [{"book_id": 1, "title": "Foo"}, {"book_id": 2, "title": "Bar"}],
        }
        zip_bytes = build_zip_of_tables(data)
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        assert set(zf.namelist()) == {"Authors.csv", "Books.csv"}
        books_csv = zf.read("Books.csv").decode("utf-8")
        assert "book_id,title" in books_csv
        assert "1,Foo" in books_csv
        assert "2,Bar" in books_csv

    def test_handles_empty_table(self):
        zip_bytes = build_zip_of_tables({"Empty": []})
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        assert zf.namelist() == ["Empty.csv"]
