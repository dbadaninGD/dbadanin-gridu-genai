import pytest

from ddl_parser import DDLParseError, parse_ddl, resolve_table_order, to_postgres_ddl


class TestParsing:
    def test_restaurant_schema_table_and_column_counts(self, restaurant_ddl):
        tables = parse_ddl(restaurant_ddl)
        assert set(tables) == {
            "Restaurants", "Customers", "Orders", "Menu",
            "Order_Items", "Reviews", "Delivery_Drivers",
        }
        assert len(tables["Orders"].columns) == 8
        assert tables["Orders"].primary_key == "order_id"

    def test_foreign_keys_are_extracted(self, restaurant_ddl):
        tables = parse_ddl(restaurant_ddl)
        fk_targets = {(fk.column, fk.ref_table) for fk in tables["Orders"].foreign_keys}
        assert fk_targets == {("customer_id", "Customers"), ("restaurant_id", "Restaurants")}

    def test_enum_values_are_parsed(self, restaurant_ddl):
        tables = parse_ddl(restaurant_ddl)
        cuisine = tables["Restaurants"].columns["cuisine_type"]
        assert cuisine.is_enum
        assert "Italian" in cuisine.enum_values
        assert "Other" in cuisine.enum_values

    def test_not_null_and_unique_flags(self, restaurant_ddl):
        tables = parse_ddl(restaurant_ddl)
        email = tables["Customers"].columns["email"]
        assert email.nullable is False
        assert email.is_unique is True

    def test_check_constraint_captured_on_column(self, restaurant_ddl):
        tables = parse_ddl(restaurant_ddl)
        rating = tables["Reviews"].columns["rating"]
        assert rating.checks, "expected the CHECK (rating >= 1 AND rating <= 5) clause to be captured"

    def test_alter_table_foreign_key_is_picked_up(self, library_ddl):
        tables = parse_ddl(library_ddl)
        branches_fks = {(fk.column, fk.ref_table) for fk in tables["Library_Branches"].foreign_keys}
        assert ("manager_id", "Employees") in branches_fks

    def test_garbage_input_raises(self):
        with pytest.raises(DDLParseError):
            parse_ddl("this is not sql at all")


class TestTableOrdering:
    def test_acyclic_schema_has_no_deferred_fks(self, restaurant_ddl):
        resolved = resolve_table_order(parse_ddl(restaurant_ddl))
        assert resolved.deferred_fks == []

    def test_acyclic_order_respects_dependencies(self, restaurant_ddl):
        resolved = resolve_table_order(parse_ddl(restaurant_ddl))
        position = {name: i for i, name in enumerate(resolved.order)}
        for name, table in resolved.tables.items():
            for fk in table.foreign_keys:
                assert position[fk.ref_table] < position[name], (
                    f"{name} depends on {fk.ref_table} but was ordered before it"
                )

    def test_self_reference_is_deferred(self, self_referencing_ddl):
        resolved = resolve_table_order(parse_ddl(self_referencing_ddl))
        assert resolved.order == ["Nodes"]
        assert len(resolved.deferred_fks) == 1
        table_name, fk = resolved.deferred_fks[0]
        assert table_name == "Nodes"
        assert fk.column == "parent_id"

    def test_two_table_cycle_defers_exactly_one_edge(self, cyclic_ddl):
        resolved = resolve_table_order(parse_ddl(cyclic_ddl))
        assert set(resolved.order) == {"A", "B"}
        assert len(resolved.deferred_fks) == 1

    def test_library_schema_cycle_is_broken_deterministically(self, library_ddl):
        # Run several times to make sure the result doesn't depend on
        # Python's hash-randomized set/dict iteration order.
        results = [resolve_table_order(parse_ddl(library_ddl)) for _ in range(5)]
        orders = {tuple(r.order) for r in results}
        deferred_sets = {
            frozenset((t, fk.column, fk.ref_table) for t, fk in r.deferred_fks) for r in results
        }
        assert len(orders) == 1, "table order should be deterministic across runs"
        assert len(deferred_sets) == 1, "deferred FK set should be deterministic across runs"

        resolved = results[0]
        assert len(resolved.order) == 9
        # Every remaining (non-deferred) FK must point to a table that
        # already comes earlier in the order.
        position = {name: i for i, name in enumerate(resolved.order)}
        deferred_cols = {(t, fk.column) for t, fk in resolved.deferred_fks}
        for name, table in resolved.tables.items():
            for fk in table.foreign_keys:
                if (name, fk.column) in deferred_cols:
                    continue
                assert position[fk.ref_table] < position[name]

        # Only genuinely cyclic edges should ever be deferred -- tables that
        # are merely transitively blocked (e.g. Book_Inventory/Book_Loans,
        # which depend on Library_Branches but aren't part of any cycle
        # themselves) must keep all of their FKs inline.
        deferred_tables = {t for t, _ in resolved.deferred_fks}
        assert "Book_Inventory" not in deferred_tables
        assert "Book_Loans" not in deferred_tables


class TestPostgresDDL:
    def test_create_statements_reference_every_table(self, restaurant_ddl):
        resolved = resolve_table_order(parse_ddl(restaurant_ddl))
        statements = to_postgres_ddl(resolved)
        joined = "\n".join(statements)
        for name in resolved.tables:
            assert f'CREATE TABLE "{name}"' in joined

    def test_deferred_fks_use_alter_table_not_inline(self, library_ddl):
        resolved = resolve_table_order(parse_ddl(library_ddl))
        statements = to_postgres_ddl(resolved)
        alter_statements = [s for s in statements if s.startswith("ALTER TABLE")]
        assert len(alter_statements) == len(resolved.deferred_fks)
        for stmt in alter_statements:
            assert "DEFERRABLE INITIALLY DEFERRED" in stmt

    def test_enum_column_becomes_varchar_with_check(self, restaurant_ddl):
        resolved = resolve_table_order(parse_ddl(restaurant_ddl))
        statements = to_postgres_ddl(resolved)
        create_restaurants = next(s for s in statements if s.startswith('CREATE TABLE "Restaurants"'))
        assert "VARCHAR(255)" in create_restaurants
        assert "CHECK" in create_restaurants
        assert "'Italian'" in create_restaurants

    def test_auto_increment_keyword_is_stripped(self, restaurant_ddl):
        resolved = resolve_table_order(parse_ddl(restaurant_ddl))
        statements = to_postgres_ddl(resolved)
        assert "AUTO_INCREMENT" not in "\n".join(statements)
