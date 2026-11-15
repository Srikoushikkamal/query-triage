import json
from pathlib import Path

import pytest

import triage

QUERY = "SELECT o.id FROM customers c JOIN orders o ON o.customer_id = c.id WHERE c.email = 'a@b.c'"


class FakeDB:
    """Stands in for psql. Timings drop once an index exists or the query is rewritten."""

    def __init__(self, slow=100.0, fast=1.0, rows_before="1\n2", rows_after=None):
        self.slow, self.fast = slow, fast
        self.rows_before = rows_before
        self.rows_after = rows_before if rows_after is None else rows_after
        self.indexed = False
        self.calls = []

    def __call__(self, sql):
        self.calls.append(sql)
        if sql.startswith("EXPLAIN"):
            fast = self.indexed or "REWRITTEN" in sql
            return f"Seq Scan on orders\nExecution Time: {self.fast if fast else self.slow} ms\n"
        if sql.startswith("SELECT tablename"):
            return "customers\norders\n"
        if sql.startswith("SELECT reltuples"):
            return "1000\n"
        if sql.startswith("SELECT column_name"):
            return "id integer\nemail text\n"
        if sql.startswith("SELECT indexdef"):
            return ""
        if sql.startswith("CREATE INDEX"):
            self.indexed = True
            return ""
        if sql.startswith("DROP INDEX"):
            self.indexed = False
            return ""
        if sql == "ANALYZE":
            return ""
        return (self.rows_after if (self.indexed or "REWRITTEN" in sql) else self.rows_before) + "\n"

    def ran(self, prefix):
        return [c for c in self.calls if c.startswith(prefix)]


def index_answer(*statements):
    return {"fix_type": "index", "statements": list(statements), "rewritten_query": None, "diagnosis": ""}


# ---- parsing helpers

def test_execution_ms():
    assert triage.execution_ms("x\nExecution Time: 12.345 ms\n") == 12.345


def test_execution_ms_missing():
    with pytest.raises(ValueError):
        triage.execution_ms("no timing here")


def test_split_queries_finds_the_three_slow_queries():
    text = (Path(__file__).parents[1] / "sql" / "03_slow_queries.sql").read_text()
    assert len(triage.split_queries(text)) == 3


def test_tables_in_reads_query_and_plan():
    plan = "Parallel Seq Scan on order_items oi\n  -> Index Scan using products_pkey on products p"
    found = triage.tables_in("SELECT 1 FROM order_items oi JOIN products p ON p.id = oi.product_id", plan)
    assert {"order_items", "products"} <= found


def test_build_prompt_has_query_plan_and_schema():
    prompt = triage.build_prompt("SELECT 1", "the plan", "Table t")
    assert "SELECT 1" in prompt and "the plan" in prompt and "Table t" in prompt


# ---- the model's answer

def test_parse_answer_with_code_fence_and_prose():
    reply = 'Sure.\n```json\n{"diagnosis": "d", "fix_type": "index", "statements": ["CREATE INDEX ON t (a)"]}\n```'
    answer = triage.parse_answer(reply)
    assert answer["fix_type"] == "index"
    assert answer["statements"] == ["CREATE INDEX ON t (a)"]
    assert answer["rewritten_query"] is None


def test_parse_answer_rejects_unknown_fix_type():
    with pytest.raises(ValueError):
        triage.parse_answer('{"fix_type": "drop_everything"}')


def test_parse_answer_rejects_no_json():
    with pytest.raises(ValueError):
        triage.parse_answer("I think you should add an index.")


# ---- which statements may run

TABLES = {"customers", "orders"}


def test_plain_index_is_allowed():
    name, sql = triage.validate_index("CREATE INDEX customers_email_idx ON customers (email)", TABLES)
    assert name == "customers_email_idx"
    assert sql == "CREATE INDEX customers_email_idx ON customers (email)"


def test_unnamed_index_gets_a_qt_name():
    name, sql = triage.validate_index("create index on orders (customer_id, placed_at);", TABLES)
    assert name == "qt_orders_customer_id_placed_at"
    assert sql == "CREATE INDEX qt_orders_customer_id_placed_at ON orders (customer_id, placed_at)"


@pytest.mark.parametrize("statement", [
    "DROP TABLE orders",
    "CREATE INDEX ON customers (email); DROP TABLE orders",
    "CREATE INDEX ON customers (email) -- hi",
    "CREATE INDEX ON customers (lower(email))",
    "CREATE INDEX ON customers USING hash (email)",
    "CREATE INDEX ON customers (email) WHERE true",
    "CREATE UNIQUE INDEX ON customers (email)",
    "CREATE INDEX ON secrets (token)",
])
def test_everything_else_is_refused(statement):
    with pytest.raises(ValueError):
        triage.validate_index(statement, TABLES)


# ---- applying a fix

def test_index_that_helps_is_kept():
    db = FakeDB()
    result = triage.apply_fix(db, QUERY, index_answer("CREATE INDEX ON customers (email)"), 100.0, TABLES)
    assert result["kept"] and result["same_results"]
    assert result["speedup"] == pytest.approx(100.0)
    assert db.indexed and not db.ran("DROP INDEX")


def test_index_that_does_not_help_is_undone():
    db = FakeDB(slow=100.0, fast=95.0)
    result = triage.apply_fix(db, QUERY, index_answer("CREATE INDEX ON customers (email)"), 100.0, TABLES)
    assert not result["kept"] and result["undone"]
    assert not db.indexed
    assert db.ran("DROP INDEX")


def test_fix_that_changes_results_is_rejected_and_undone():
    db = FakeDB(rows_before="1\n2", rows_after="1")
    result = triage.apply_fix(db, QUERY, index_answer("CREATE INDEX ON customers (email)"), 100.0, TABLES)
    assert not result["kept"] and not result["same_results"]
    assert not db.indexed


def test_row_order_does_not_matter_for_the_comparison():
    db = FakeDB(rows_before="1\n2\n3", rows_after="3\n1\n2")
    result = triage.apply_fix(db, QUERY, index_answer("CREATE INDEX ON customers (email)"), 100.0, TABLES)
    assert result["same_results"] and result["kept"]


def test_a_bad_statement_in_the_list_runs_nothing():
    db = FakeDB()
    answer = index_answer("CREATE INDEX ON customers (email)", "DROP TABLE orders")
    with pytest.raises(ValueError):
        triage.apply_fix(db, QUERY, answer, 100.0, TABLES)
    assert not db.ran("CREATE INDEX") and not db.ran("DROP TABLE")


def test_rewrite_is_timed_and_compared():
    db = FakeDB()
    answer = {"fix_type": "rewrite", "statements": [], "diagnosis": "",
              "rewritten_query": "SELECT 1 /* REWRITTEN */"}
    result = triage.apply_fix(db, QUERY, answer, 100.0, TABLES)
    assert result["kept"] and result["same_results"]
    assert not db.ran("CREATE INDEX")


def test_none_changes_nothing():
    db = FakeDB()
    answer = {"fix_type": "none", "statements": [], "diagnosis": "", "rewritten_query": None}
    assert triage.apply_fix(db, QUERY, answer, 100.0, TABLES)["applied"] is False


# ---- the whole flow

def llm_returning(answer):
    return lambda system, user: json.dumps(answer)


def test_triage_without_apply_changes_nothing():
    db = FakeDB()
    lines = []
    answer, result = triage.triage(db, QUERY, llm_returning(index_answer("CREATE INDEX ON customers (email)")),
                                   apply=False, out=lines.append)
    assert result is None and not db.ran("CREATE INDEX")
    assert any("--apply" in line for line in lines)


def test_triage_with_apply_reports_both_timings():
    db = FakeDB()
    lines = []
    _, result = triage.triage(db, QUERY, llm_returning(index_answer("CREATE INDEX ON customers (email)")),
                              apply=True, out=lines.append)
    assert result["kept"]
    assert any("KEPT" in line for line in lines)


def test_the_prompt_sent_to_the_model_has_the_plan_and_schema():
    seen = {}

    def llm(system, user):
        seen["system"], seen["user"] = system, user
        return json.dumps(index_answer("CREATE INDEX ON customers (email)"))

    triage.triage(FakeDB(), QUERY, llm, apply=False, out=lambda *_: None)
    assert "Seq Scan on orders" in seen["user"]
    assert "Table customers (about 1000 rows)" in seen["user"]
    assert "indexes: none" in seen["user"]
    assert "JSON" in seen["system"]
