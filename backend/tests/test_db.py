import io
import zipfile
from unittest.mock import MagicMock

import pytest

import db
from ddl_parser import parse_ddl, resolve_table_order


@pytest.fixture
def restaurant_table(restaurant_ddl):
    return resolve_table_order(parse_ddl(restaurant_ddl)).tables["Restaurants"]


class TestInsertSql:
    def test_quotes_identifiers_and_uses_bind_params(self):
        sql = db._insert_sql("Authors", ["author_id", "first_name"])
        assert sql == 'INSERT INTO "Authors" ("author_id", "first_name") VALUES (:author_id, :first_name)'


class TestSchemaDescription:
    def test_identifiers_are_quoted_for_the_model(self):
        # The NL->SQL model must see quoted, case-preserved identifiers so it
        # copies them verbatim; unquoted mixed-case names fail in Postgres.
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [
            ("Customers", "customer_id", "integer"),
            ("Customers", "city", "character varying"),
        ]
        desc = db.get_live_schema_description(conn)
        assert 'Table "Customers"' in desc
        assert '"customer_id" (integer)' in desc
        assert '"city" (character varying)' in desc


class TestReadOnlyGuard:
    @pytest.mark.parametrize("query", [
        "SELECT * FROM Orders",
        "  select id from orders",
        "WITH recent AS (SELECT 1) SELECT * FROM recent",
    ])
    def test_accepts_reads(self, query):
        assert db.is_read_only_query(query) is True

    @pytest.mark.parametrize("query", [
        "DROP TABLE Orders",
        "DELETE FROM Orders",
        "UPDATE Orders SET total_amount = 0",
        "INSERT INTO Orders VALUES (1)",
        "; SELECT 1; DROP TABLE Orders;",
    ])
    def test_rejects_writes(self, query):
        assert db.is_read_only_query(query) is False


class TestZipExport:
    def test_builds_one_csv_per_table(self):
        zip_bytes = db.build_zip_of_all_tables({
            "Restaurants": [{"restaurant_id": 1, "name": "Pasta Place"}],
            "Customers": [],
        })
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        assert set(zf.namelist()) == {"Restaurants.csv", "Customers.csv"}
        content = zf.read("Restaurants.csv").decode()
        assert "restaurant_id,name" in content
        assert "1,Pasta Place" in content


class TestUpdateRow:
    def test_builds_parameterized_update_with_pk_in_where(self, restaurant_table):
        engine = MagicMock()
        conn = engine.begin.return_value.__enter__.return_value

        db.update_row(engine, restaurant_table, 42, {"name": "New Name", "rating": 4.5})

        conn.execute.assert_called_once()
        (sql_arg, params_arg), _ = conn.execute.call_args
        sql_text = str(sql_arg)
        assert 'UPDATE "Restaurants" SET' in sql_text
        assert '"name" = :name' in sql_text
        assert 'WHERE "restaurant_id" = :__pk' in sql_text
        assert params_arg == {"name": "New Name", "rating": 4.5, "__pk": 42}

    def test_noop_when_no_values(self, restaurant_table):
        engine = MagicMock()
        db.update_row(engine, restaurant_table, 42, {})
        engine.begin.assert_not_called()


class TestSchemaCreation:
    def test_executes_every_statement_in_order(self, restaurant_ddl):
        resolved = resolve_table_order(parse_ddl(restaurant_ddl))
        engine = MagicMock()
        conn = engine.begin.return_value.__enter__.return_value

        db.create_schema(engine, resolved)

        executed = [str(call.args[0]) for call in conn.execute.call_args_list]
        assert any(stmt.startswith("DROP TABLE") for stmt in executed)
        assert any(stmt.startswith("CREATE TABLE") for stmt in executed)


class TestInsertRows:
    def test_inserts_in_dependency_order_within_one_transaction(self, restaurant_ddl):
        resolved = resolve_table_order(parse_ddl(restaurant_ddl))
        engine = MagicMock()
        conn = engine.begin.return_value.__enter__.return_value

        data = {name: [{"id": 1}] for name in resolved.order}
        db.insert_rows(engine, resolved, data)

        # engine.begin() used exactly once -> everything in one transaction.
        assert engine.begin.call_count == 1
        assert conn.execute.call_count == len(resolved.order)

    def test_skips_tables_with_no_rows(self, restaurant_ddl):
        resolved = resolve_table_order(parse_ddl(restaurant_ddl))
        engine = MagicMock()
        conn = engine.begin.return_value.__enter__.return_value

        data = {resolved.order[0]: [{"id": 1}]}
        db.insert_rows(engine, resolved, data)

        assert conn.execute.call_count == 1
