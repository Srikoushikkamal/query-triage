"""Time the three slow queries before and after the fixes.

Resets the database to its slow state, times each query, applies the fixes,
times them again and prints a table. Each timing is the median of several runs
of EXPLAIN (ANALYZE), read from the "Execution Time" line, after one warm-up.

By default it calls the local `psql`. To go through Docker instead:

    PSQL="docker compose exec -T db psql -U postgres -d shop" python bench.py
"""
import os
import re
import shlex
import statistics
import subprocess
from pathlib import Path

SQL = Path(__file__).parent / "sql"
PSQL = shlex.split(os.environ.get("PSQL", "psql -U postgres -d shop"))
RUNS = 7

# The "after" for Q2 is a rewritten query, not an index (see sql/99_fixes.sql).
Q2_FIXED = """SELECT count(*) FROM orders
WHERE placed_at >= timestamptz '2025-01-15' AND placed_at < timestamptz '2025-01-16'"""


def psql(sql):
    result = subprocess.run(
        PSQL + ["-X", "-q", "-v", "ON_ERROR_STOP=1"],
        input=sql, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or "psql failed")
    return result.stdout


def load_queries():
    text = (SQL / "03_slow_queries.sql").read_text()
    text = re.sub(r"--[^\n]*", "", text)
    statements = [s.strip() for s in text.split(";") if s.strip()]
    if len(statements) != 3:
        raise SystemExit(f"expected 3 queries in 03_slow_queries.sql, found {len(statements)}")
    return statements


def time_ms(query):
    def once():
        out = psql("EXPLAIN (ANALYZE) " + query)
        return float(re.search(r"Execution Time: ([\d.]+) ms", out).group(1))
    once()  # warm-up
    return statistics.median(once() for _ in range(RUNS))


def main():
    q1, q2, q3 = load_queries()
    psql((SQL / "00_reset.sql").read_text())
    psql("ANALYZE")
    before = [time_ms(q) for q in (q1, q2, q3)]

    psql((SQL / "99_fixes.sql").read_text())
    psql("ANALYZE")
    after = [time_ms(q) for q in (q1, Q2_FIXED, q3)]

    print(f"{'':4}{'before':>12}{'after':>12}{'speed-up':>10}")
    for name, b, a in zip(("Q1", "Q2", "Q3"), before, after):
        print(f"{name:4}{b:>9.2f} ms{a:>9.2f} ms{b / a:>9.1f}x")


if __name__ == "__main__":
    main()
