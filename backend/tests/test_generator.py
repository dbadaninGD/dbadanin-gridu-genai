import json

import pytest

from ddl_parser import parse_ddl, resolve_table_order
from generator import (
    GenerationContext,
    _extract_json_array,
    _salvage_json_objects,
    assign_foreign_keys,
    assign_primary_keys,
    build_prompt,
    compute_pk_pools,
    enforce_uniqueness,
    fk_columns,
    generate_all_tables,
    generate_table_rows,
    generated_columns,
)


class TestJsonParsing:
    def test_parses_clean_array(self):
        assert _extract_json_array('[{"x": 1}, {"x": 2}]') == [{"x": 1}, {"x": 2}]

    def test_strips_markdown_fence(self):
        assert _extract_json_array('```json\n[{"x": 1}]\n```') == [{"x": 1}]

    def test_unwraps_rows_envelope(self):
        assert _extract_json_array('{"rows": [{"x": 1}]}') == [{"x": 1}]

    def test_salvages_truncated_array(self):
        # Last object cut off mid-string (the classic out-of-tokens failure).
        truncated = '[{"a": 1, "b": "hello"}, {"a": 2, "b": "wor'
        assert _extract_json_array(truncated) == [{"a": 1, "b": "hello"}]

    def test_salvage_respects_escaped_quotes(self):
        tricky = '[{"note": "she said \\"hi\\" today"}, {"note": "trunc'
        assert _extract_json_array(tricky) == [{"note": 'she said "hi" today'}]

    def test_salvage_helper_returns_all_complete_objects(self):
        text = '[{"a":1},{"a":2},{"a":3}]'
        assert _salvage_json_objects(text) == [{"a": 1}, {"a": 2}, {"a": 3}]

    def test_unrecoverable_still_raises(self):
        with pytest.raises((ValueError, json.JSONDecodeError)):
            _extract_json_array("this is not json and has no objects")


@pytest.fixture
def resolved_restaurant(restaurant_ddl):
    return resolve_table_order(parse_ddl(restaurant_ddl))


@pytest.fixture
def resolved_library(library_ddl):
    return resolve_table_order(parse_ddl(library_ddl))


def echo_model(prompt: str) -> str:
    """A fake `call_model` that returns one row per requested column, with
    deterministic placeholder values, entirely from the prompt text -- no
    real LLM involved."""
    import re

    n = int(re.search(r"Generate exactly (\d+) realistic", prompt).group(1))
    cols = re.findall(r'- "(\w+)":', prompt)
    return json.dumps([{c: f"{c}_{i}" for c in cols} for i in range(n)])


class TestColumnSelection:
    def test_pk_and_fk_columns_excluded_from_generated_columns(self, resolved_restaurant):
        orders = resolved_restaurant.tables["Orders"]
        names = {c.name for c in generated_columns(orders)}
        assert "order_id" not in names  # primary key
        assert "customer_id" not in names  # foreign key
        assert "restaurant_id" not in names  # foreign key
        assert "payment_method" in names  # a normal business column

    def test_fk_columns_helper(self, resolved_restaurant):
        assert fk_columns(resolved_restaurant.tables["Orders"]) == {"customer_id", "restaurant_id"}


class TestPromptBuilding:
    def test_prompt_mentions_constraints(self, resolved_restaurant):
        ctx = GenerationContext(rows_per_table=10)
        prompt = build_prompt(resolved_restaurant.tables["Restaurants"], resolved_restaurant, ctx, 0, 10)
        assert "cuisine_type" in prompt
        assert "Italian" in prompt  # enum values surfaced
        assert "NOT NULL" in prompt

    def test_prompt_lists_fk_columns_as_excluded(self, resolved_restaurant):
        ctx = GenerationContext(rows_per_table=5)
        prompt = build_prompt(resolved_restaurant.tables["Orders"], resolved_restaurant, ctx, 0, 5)
        assert "customer_id" in prompt
        assert "do NOT include it in your output" in prompt

    def test_extra_instructions_are_included(self, resolved_restaurant):
        ctx = GenerationContext(rows_per_table=5, extra_instructions="Make all prices under $20")
        prompt = build_prompt(resolved_restaurant.tables["Menu"], resolved_restaurant, ctx, 0, 5)
        assert "Make all prices under $20" in prompt


class TestKeyAssignment:
    def test_primary_keys_are_sequential(self, resolved_restaurant):
        rows = [{} for _ in range(4)]
        assign_primary_keys(rows, resolved_restaurant.tables["Restaurants"])
        assert [r["restaurant_id"] for r in rows] == [1, 2, 3, 4]

    def test_foreign_keys_are_sampled_from_pool(self, resolved_restaurant):
        rows = [{} for _ in range(20)]
        pools = {"Customers": [1, 2, 3], "Restaurants": [1]}
        assign_foreign_keys(rows, resolved_restaurant.tables["Orders"], pools)
        assert all(r["customer_id"] in {1, 2, 3} for r in rows)
        assert all(r["restaurant_id"] == 1 for r in rows)

    def test_compute_pk_pools_covers_every_table(self, resolved_restaurant):
        pools = compute_pk_pools(resolved_restaurant, rows_per_table=7)
        assert set(pools) == set(resolved_restaurant.tables)
        assert pools["Restaurants"] == list(range(1, 8))


class TestUniquenessRepair:
    def test_duplicate_unique_values_are_disambiguated(self, resolved_restaurant):
        table = resolved_restaurant.tables["Customers"]
        rows = [
            {"customer_id": 1, "email": "a@x.com"},
            {"customer_id": 2, "email": "a@x.com"},
            {"customer_id": 3, "email": "b@x.com"},
        ]
        enforce_uniqueness(rows, table)
        emails = [r["email"] for r in rows]
        assert len(emails) == len(set(emails)), "email values must be unique after repair"
        assert emails[0] == "a@x.com"  # first occurrence untouched


class TestGeneration:
    def test_batching_splits_large_requests(self, resolved_restaurant):
        calls = []

        def counting_model(prompt):
            calls.append(prompt)
            return echo_model(prompt)

        ctx = GenerationContext(rows_per_table=7, batch_size=3)
        pools = compute_pk_pools(resolved_restaurant, 7)
        rows = generate_table_rows(
            resolved_restaurant.tables["Restaurants"], resolved_restaurant, ctx, pools, counting_model
        )
        assert len(rows) == 7
        assert len(calls) == 3  # 3 + 3 + 1

    def test_short_batch_is_topped_up_not_padded(self, resolved_restaurant):
        # A model that always returns one fewer row than asked. The generator
        # should keep asking until it reaches the target, never duplicate-pad,
        # and always end with exactly rows_per_table rows.
        def short_model(prompt):
            import re as _re
            n = int(_re.search(r"Generate exactly (\d+) realistic", prompt).group(1))
            n = max(1, n - 1)  # always one short
            cols = _re.findall(r'- "(\w+)":', prompt)
            return json.dumps([{c: f"{c}_{i}" for c in cols} for i in range(n)])

        ctx = GenerationContext(rows_per_table=5, batch_size=5)
        pools = compute_pk_pools(resolved_restaurant, 5)
        rows = generate_table_rows(
            resolved_restaurant.tables["Restaurants"], resolved_restaurant, ctx, pools, short_model
        )
        assert len(rows) == 5
        # Primary keys must still be a clean 1..5 with no duplicates.
        assert sorted(r["restaurant_id"] for r in rows) == [1, 2, 3, 4, 5]

    def test_unparseable_batch_retries_then_gives_up_gracefully(self, resolved_restaurant):
        # A model that returns junk always: the generator retries once at a
        # smaller size, then returns whatever it has (here nothing) rather than
        # raising and 500-ing the whole request.
        def junk_model(prompt):
            return "not json at all"

        ctx = GenerationContext(rows_per_table=4, batch_size=4)
        pools = compute_pk_pools(resolved_restaurant, 4)
        rows = generate_table_rows(
            resolved_restaurant.tables["Restaurants"], resolved_restaurant, ctx, pools, junk_model
        )
        assert rows == []  # graceful: no exception, just an empty table

    def test_end_to_end_referential_integrity(self, resolved_library):
        ctx = GenerationContext(rows_per_table=5, batch_size=50)
        data = generate_all_tables(resolved_library, ctx, echo_model)

        assert set(data) == set(resolved_library.tables)
        for name, rows in data.items():
            assert len(rows) == 5

        pk_pools = {name: {row[resolved_library.tables[name].primary_key] for row in rows}
                    for name, rows in data.items()}

        for name, table in resolved_library.tables.items():
            for fk in table.foreign_keys:
                valid_ids = set(range(1, 6))  # every table has PKs 1..5
                for row in data[name]:
                    assert row[fk.column] in valid_ids, (
                        f"{name}.{fk.column}={row[fk.column]} is not a valid {fk.ref_table} id"
                    )

    def test_row_count_is_configurable(self, resolved_restaurant):
        ctx = GenerationContext(rows_per_table=1, batch_size=50)
        data = generate_all_tables(resolved_restaurant, ctx, echo_model)
        assert all(len(rows) == 1 for rows in data.values())
