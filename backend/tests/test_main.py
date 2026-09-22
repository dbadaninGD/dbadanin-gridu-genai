import json
import zipfile
import io
import re

import pytest
from fastapi.testclient import TestClient

import main
import db
from ddl_parser import parse_ddl


SIMPLE_DDL = """
CREATE TABLE Authors (
    author_id INT PRIMARY KEY AUTO_INCREMENT,
    first_name VARCHAR(100) NOT NULL,
    last_name VARCHAR(100) NOT NULL
);
CREATE TABLE Books (
    book_id INT PRIMARY KEY AUTO_INCREMENT,
    title VARCHAR(255) NOT NULL,
    author_id INT NOT NULL,
    FOREIGN KEY (author_id) REFERENCES Authors(author_id)
);
"""


def echo_model_response(prompt: str) -> str:
    n = int(re.search(r"Generate exactly (\d+) realistic", prompt).group(1))
    cols = re.findall(r'- "(\w+)":', prompt)
    return json.dumps([{c: f"{c}_{i}" for c in cols} for i in range(n)])


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def no_real_database(monkeypatch):
    """Every test in this module runs against a fake DB layer -- none of
    them need (or should) touch a real Postgres instance."""
    monkeypatch.setattr(main.db, "create_schema", lambda engine, resolved: None)
    monkeypatch.setattr(main.db, "insert_rows", lambda engine, resolved, data: None)
    monkeypatch.setattr(main.db, "update_row", lambda engine, table, pk, values: None)
    yield


@pytest.fixture(autouse=True)
def reset_schema_cache():
    main._SCHEMA_CACHE.clear()
    yield
    main._SCHEMA_CACHE.clear()


class TestGenerateEndpoint:
    def test_missing_ddl_schema_is_rejected(self, client):
        response = client.post("/api/generate", json={"prompt": "test"})
        assert response.status_code == 422  # ddl_schema is a required field

    def test_invalid_ddl_returns_400(self, client):
        response = client.post("/api/generate", json={"ddl_schema": "not sql"})
        assert response.status_code == 400

    def test_happy_path_returns_generated_data_with_integrity(self, client, monkeypatch):
        monkeypatch.setattr(main, "_call_gemini_for_rows", lambda prompt, temp, tok: echo_model_response(prompt))

        response = client.post("/api/generate", json={
            "prompt": "",
            "ddl_schema": SIMPLE_DDL,
            "rows_per_table": 4,
        })

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "success"
        assert len(body["data"]["Authors"]) == 4
        assert len(body["data"]["Books"]) == 4
        valid_author_ids = {row["author_id"] for row in body["data"]["Authors"]}
        assert valid_author_ids == {1, 2, 3, 4}
        for row in body["data"]["Books"]:
            assert row["author_id"] in valid_author_ids

    def test_rows_per_table_is_capped(self, client):
        response = client.post("/api/generate", json={
            "ddl_schema": SIMPLE_DDL,
            "rows_per_table": 100000,
        })
        assert response.status_code == 422

    def test_model_returning_unparseable_json_is_a_502(self, client, monkeypatch):
        monkeypatch.setattr(main, "_call_gemini_for_rows", lambda prompt, temp, tok: "not json at all")
        response = client.post("/api/generate", json={"ddl_schema": SIMPLE_DDL, "rows_per_table": 2})
        assert response.status_code == 502


class TestRefineEndpoint:
    def test_unknown_table_is_404(self, client):
        response = client.post("/api/refine", json={
            "table_name": "Nope", "instructions": "x", "current_data": [],
        })
        assert response.status_code == 404

    def test_protected_columns_cannot_be_overwritten_by_the_model(self, client, monkeypatch):
        main._SCHEMA_CACHE.update(parse_ddl(SIMPLE_DDL))

        def sneaky_refine(instructions, editable_rows):
            # Try to smuggle in a primary-key change; should be discarded.
            return [{"first_name": "Changed", "last_name": "Also Changed", "author_id": 999} for _ in editable_rows]

        monkeypatch.setattr(main, "_call_gemini_for_refine", sneaky_refine)

        response = client.post("/api/refine", json={
            "table_name": "Authors",
            "instructions": "rename everyone",
            "current_data": [
                {"author_id": 1, "first_name": "A", "last_name": "B"},
                {"author_id": 2, "first_name": "C", "last_name": "D"},
            ],
        })

        assert response.status_code == 200
        rows = response.json()["data"]
        assert [r["author_id"] for r in rows] == [1, 2]  # untouched despite the model's attempt

    def test_mismatched_row_count_is_a_502(self, client, monkeypatch):
        main._SCHEMA_CACHE.update(parse_ddl(SIMPLE_DDL))
        monkeypatch.setattr(main, "_call_gemini_for_refine", lambda instructions, rows: [])

        response = client.post("/api/refine", json={
            "table_name": "Authors",
            "instructions": "x",
            "current_data": [{"author_id": 1, "first_name": "A", "last_name": "B"}],
        })
        assert response.status_code == 502


class _FakeConn:
    """A stand-in for a SQLAlchemy connection: `execute` runs a caller-supplied
    function (which may raise), and `rollback` is counted."""
    def __init__(self, execute_fn):
        self._execute_fn = execute_fn
        self.rollbacks = 0

    def execute(self, sql):
        return self._execute_fn(str(sql))

    def rollback(self):
        self.rollbacks += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeSqlResult:
    def __init__(self, columns, rows):
        self._columns = columns
        self._rows = rows

    def keys(self):
        return self._columns

    def fetchall(self):
        return self._rows


class TestQueryEndpoint:
    def test_no_data_yet_is_400(self, client, monkeypatch):
        monkeypatch.setattr(main.engine, "connect", lambda: _FakeConn(lambda sql: None))
        monkeypatch.setattr(db, "get_live_schema_description", lambda conn: "")
        response = client.post("/api/query", json={"question": "how many books?"})
        assert response.status_code == 400

    def test_non_select_sql_is_rejected(self, client, monkeypatch):
        monkeypatch.setattr(main.engine, "connect", lambda: _FakeConn(lambda sql: None))
        monkeypatch.setattr(db, "get_live_schema_description", lambda conn: 'Table "Authors"\nColumns: "author_id" (integer)')
        monkeypatch.setattr(main, "_call_gemini_for_sql", lambda question, schema, prior_error=None: 'DROP TABLE "Authors";')
        response = client.post("/api/query", json={"question": "delete everything"})
        assert response.status_code == 400

    def test_happy_path_returns_rows(self, client, monkeypatch):
        conn = _FakeConn(lambda sql: _FakeSqlResult(["name"], [("Acme",)]))
        monkeypatch.setattr(main.engine, "connect", lambda: conn)
        monkeypatch.setattr(db, "get_live_schema_description", lambda c: 'Table "Companies"\nColumns: "name" (varchar)')
        monkeypatch.setattr(main, "_call_gemini_for_sql", lambda q, s, prior_error=None: 'SELECT "name" FROM "Companies"')
        response = client.post("/api/query", json={"question": "list companies"})
        assert response.status_code == 200
        assert response.json()["data"] == [{"name": "Acme"}]

    def test_failed_sql_is_self_corrected_on_retry(self, client, monkeypatch):
        # First execution raises (a bad alias); second succeeds. The endpoint
        # should feed the error back to the model and return the corrected rows.
        state = {"execs": 0}

        def execute_fn(sql):
            state["execs"] += 1
            if state["execs"] == 1:
                raise Exception('missing FROM-clause entry for table "t1"')
            return _FakeSqlResult(["name"], [("Acme",)])

        conn = _FakeConn(execute_fn)
        monkeypatch.setattr(main.engine, "connect", lambda: conn)
        monkeypatch.setattr(db, "get_live_schema_description", lambda c: 'Table "Companies"\nColumns: "name" (varchar)')

        def sql_model(question, schema, prior_error=None):
            if prior_error is None:
                return 'SELECT T1."name" FROM "Companies"'  # broken
            return 'SELECT "name" FROM "Companies"'  # corrected

        monkeypatch.setattr(main, "_call_gemini_for_sql", sql_model)

        response = client.post("/api/query", json={"question": "list companies"})
        assert response.status_code == 200
        assert response.json()["data"] == [{"name": "Acme"}]
        assert response.json()["query_executed"] == 'SELECT "name" FROM "Companies"'
        assert conn.rollbacks == 1  # rolled back the failed attempt before retrying

    def test_both_attempts_failing_returns_400(self, client, monkeypatch):
        def execute_fn(sql):
            raise Exception("boom")

        conn = _FakeConn(execute_fn)
        monkeypatch.setattr(main.engine, "connect", lambda: conn)
        monkeypatch.setattr(db, "get_live_schema_description", lambda c: 'Table "Companies"\nColumns: "name" (varchar)')
        monkeypatch.setattr(main, "_call_gemini_for_sql", lambda q, s, prior_error=None: 'SELECT bad FROM "Companies"')
        response = client.post("/api/query", json={"question": "x"})
        assert response.status_code == 400
        assert "boom" in response.json()["detail"]


class TestDownloadAllEndpoint:
    def test_no_data_yet_is_404(self, client):
        response = client.get("/api/download/all")
        assert response.status_code == 404

    def test_returns_a_zip_with_one_csv_per_table(self, client, monkeypatch):
        main._SCHEMA_CACHE.update(parse_ddl(SIMPLE_DDL))
        monkeypatch.setattr(db, "fetch_table_rows", lambda engine, name: [{"id": 1}])

        response = client.get("/api/download/all")

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        zf = zipfile.ZipFile(io.BytesIO(response.content))
        assert set(zf.namelist()) == {"Authors.csv", "Books.csv"}


class TestHealthz:
    def test_ok(self, client):
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
