"""Diagnose a slow Postgres query with an LLM, then prove the fix by re-timing it.

    python triage.py --query-file sql/03_slow_queries.sql --number 1
    python triage.py --query-file sql/03_slow_queries.sql --number 1 --apply

It runs EXPLAIN (ANALYZE, BUFFERS), collects the schema, indexes and row counts
of the tables involved, and sends all of it to an LLM. With --apply it applies
the suggested fix, re-times the query, checks the results did not change, and
undoes the fix if the query did not get meaningfully faster.

Talks to Postgres through `psql`, like bench.py. To go through Docker:

    PSQL="docker compose exec -T db psql -U postgres -d shop" python triage.py ...

The LLM is called with ANTHROPIC_API_KEY (model from TRIAGE_MODEL). Use
--prompt-only to see what would be sent without calling anything, or
--response FILE to replay a saved answer.
"""
import argparse
import json
import os
import re
import shlex
import statistics
import subprocess
import sys
import urllib.request
from pathlib import Path

DEFAULT_MODEL = "claude-sonnet-5-5"
RUNS = 5
MIN_SPEEDUP = 1.25

SYSTEM_PROMPT = """You are a PostgreSQL performance engineer. You are given a slow query, its \
EXPLAIN (ANALYZE, BUFFERS) output, and the definitions, indexes and row counts of the \
tables involved. Work out why it is slow and propose the smallest fix.

Reply with one JSON object and nothing else:
{
  "diagnosis": "2-4 sentences naming the plan node that costs the time and why",
  "fix_type": "index" | "rewrite" | "none",
  "statements": ["CREATE INDEX ..."],
  "rewritten_query": "..." or null
}

Rules:
- fix_type "index": statements holds plain `CREATE INDEX name ON table (col, ...)` \
statements only, no other SQL, no semicolons. rewritten_query is null.
- fix_type "rewrite": use it when an index cannot help, for example when a function \
wraps an indexed column in the WHERE clause. rewritten_query must return exactly the \
same rows as the original. statements is empty.
- fix_type "none": the query is already reasonable. statements is empty.
- Do not suggest indexes that already exist."""


# ---------------------------------------------------------------- psql

def make_runner(psql_cmd=None):
    cmd = shlex.split(psql_cmd or os.environ.get("PSQL", "psql -U postgres -d shop"))

    def run(sql):
        result = subprocess.run(
            cmd + ["-X", "-q", "-At", "-v", "ON_ERROR_STOP=1"],
            input=sql, capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "psql failed")
        return result.stdout

    return run


# ---------------------------------------------------------------- queries

def split_queries(text):
    text = re.sub(r"--[^\n]*", "", text)
    return [s.strip() for s in text.split(";") if s.strip()]


def execution_ms(plan_text):
    match = re.search(r"Execution Time: ([\d.]+) ms", plan_text)
    if not match:
        raise ValueError("no 'Execution Time' in the plan output")
    return float(match.group(1))


def time_query(run, query, runs=RUNS):
    """Median execution time in ms over several EXPLAIN ANALYZE runs, after a warm-up."""
    def once():
        return execution_ms(run("EXPLAIN (ANALYZE) " + query))
    once()
    return statistics.median(once() for _ in range(runs))


def result_rows(run, query):
    """The query's output as a sorted list of lines, so row order does not matter."""
    return sorted(run(query).splitlines())


# ---------------------------------------------------------------- context

def tables_in(query, plan_text):
    names = set(re.findall(r"\b(?:from|join)\s+([a-z_][a-z0-9_]*)", query, re.I))
    names |= set(re.findall(r"\bon ([a-z_][a-z0-9_]*)\b", plan_text))
    return names


def collect_context(run, query, plan_text):
    existing = set(run("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").split())
    tables = sorted(t.lower() for t in tables_in(query, plan_text) if t.lower() in existing)
    lines = []
    for table in tables:
        rows = run(f"SELECT reltuples::bigint FROM pg_class WHERE relname = '{table}'").strip()
        columns = run(
            "SELECT column_name || ' ' || data_type FROM information_schema.columns "
            f"WHERE table_schema = 'public' AND table_name = '{table}' ORDER BY ordinal_position"
        ).splitlines()
        indexes = run(f"SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = '{table}'").splitlines()
        lines.append(f"Table {table} (about {rows} rows)")
        lines.append("  columns: " + ", ".join(columns))
        if indexes:
            lines.extend("  index: " + i for i in indexes)
        else:
            lines.append("  indexes: none")
    return tables, "\n".join(lines)


def build_prompt(query, plan_text, context_text):
    return (
        f"Query:\n{query}\n\n"
        f"EXPLAIN (ANALYZE, BUFFERS):\n{plan_text.strip()}\n\n"
        f"Schema:\n{context_text}\n"
    )


# ---------------------------------------------------------------- LLM

def anthropic_llm(system, user):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ANTHROPIC_API_KEY is not set. Use --prompt-only to see the prompt, "
                 "or --response FILE to replay a saved answer.")
    body = json.dumps({
        "model": os.environ.get("TRIAGE_MODEL", DEFAULT_MODEL),
        "max_tokens": 1024,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode()
    request = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)
    return "".join(block.get("text", "") for block in payload["content"])


def parse_answer(text):
    """The JSON object in the model's reply, checked for the fields we rely on."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("the reply contained no JSON object")
    answer = json.loads(text[start:end + 1])
    if answer.get("fix_type") not in ("index", "rewrite", "none"):
        raise ValueError(f"unknown fix_type: {answer.get('fix_type')!r}")
    answer.setdefault("diagnosis", "")
    answer["statements"] = answer.get("statements") or []
    answer.setdefault("rewritten_query", None)
    return answer


# ---------------------------------------------------------------- fixes

INDEX_RE = re.compile(
    r"^create\s+index\s+(?:(?!on\b)(\w+)\s+)?on\s+(\w+)\s*\(([\w\s,]+)\)$", re.I
)


def validate_index(statement, known_tables):
    """Only a plain CREATE INDEX on existing columns is allowed. Returns (name, sql)."""
    sql = statement.strip().rstrip(";").strip()
    if ";" in sql or "--" in sql or "/*" in sql:
        raise ValueError(f"refusing statement: {statement!r}")
    match = INDEX_RE.match(sql)
    if not match:
        raise ValueError(f"only plain CREATE INDEX statements are applied, got: {statement!r}")
    name, table, cols = match.groups()
    columns = [c.strip() for c in cols.split(",")]
    if not all(re.fullmatch(r"\w+", c) for c in columns):
        raise ValueError(f"refusing index columns: {cols!r}")
    if table.lower() not in known_tables:
        raise ValueError(f"index on a table that is not part of this query: {table}")
    name = name or f"qt_{table}_{'_'.join(columns)}".lower()
    return name, f"CREATE INDEX {name} ON {table} ({', '.join(columns)})"


def apply_fix(run, query, answer, before_ms, known_tables, min_speedup=MIN_SPEEDUP):
    """Apply the suggested fix and return a result dict. Undoes it if it did not help."""
    before_rows = result_rows(run, query)

    if answer["fix_type"] == "none":
        return {"applied": False, "reason": "the model found nothing to fix"}

    if answer["fix_type"] == "rewrite":
        new_query = (answer.get("rewritten_query") or "").strip().rstrip(";")
        if not new_query:
            return {"applied": False, "reason": "rewrite requested but no query given"}
        same = result_rows(run, new_query) == before_rows
        after_ms = time_query(run, new_query)
        return _verdict(before_ms, after_ms, same, min_speedup, kept_label="rewritten query is faster",
                        undo=None, extra={"query": new_query})

    checked = [validate_index(s, known_tables) for s in answer["statements"]]
    if not checked:
        return {"applied": False, "reason": "index fix requested but no statements given"}
    created = []
    try:
        for name, sql in checked:
            run(sql)
            created.append(name)
        run("ANALYZE")
        same = result_rows(run, query) == before_rows
        after_ms = time_query(run, query)
    except Exception:
        for name in created:
            run(f"DROP INDEX IF EXISTS {name}")
        raise

    def undo():
        for name in created:
            run(f"DROP INDEX IF EXISTS {name}")
        run("ANALYZE")

    return _verdict(before_ms, after_ms, same, min_speedup, kept_label="index kept", undo=undo,
                    extra={"created": created})


def _verdict(before_ms, after_ms, same, min_speedup, kept_label, undo, extra):
    speedup = before_ms / after_ms if after_ms else float("inf")
    result = {"applied": True, "before_ms": before_ms, "after_ms": after_ms,
              "speedup": speedup, "same_results": same, **extra}
    if not same:
        result["kept"] = False
        result["reason"] = "results changed, fix rejected"
    elif speedup < min_speedup:
        result["kept"] = False
        result["reason"] = f"only {speedup:.2f}x faster, below the {min_speedup}x threshold"
    else:
        result["kept"] = True
        result["reason"] = kept_label
    if not result["kept"] and undo:
        undo()
        result["undone"] = True
    return result


# ---------------------------------------------------------------- main

def get_query(args):
    if args.query:
        return args.query.strip().rstrip(";")
    if not args.query_file:
        sys.exit("give --query or --query-file")
    queries = split_queries(Path(args.query_file).read_text())
    if not 1 <= args.number <= len(queries):
        sys.exit(f"{args.query_file} has {len(queries)} queries, asked for number {args.number}")
    return queries[args.number - 1]


def triage(run, query, llm, apply=False, min_speedup=MIN_SPEEDUP, out=print):
    plan = run("EXPLAIN (ANALYZE, BUFFERS) " + query)
    known_tables, context = collect_context(run, query, plan)
    prompt = build_prompt(query, plan, context)
    answer = parse_answer(llm(SYSTEM_PROMPT, prompt))

    out(f"Diagnosis: {answer['diagnosis']}")
    out(f"Fix type:  {answer['fix_type']}")
    for statement in answer["statements"]:
        out(f"  {statement}")
    if answer["rewritten_query"]:
        out(f"  {answer['rewritten_query']}")
    if not apply:
        out("Run again with --apply to apply the fix and time it.")
        return answer, None

    before_ms = time_query(run, query)
    result = apply_fix(run, query, answer, before_ms, set(known_tables), min_speedup)
    if not result["applied"]:
        out(result["reason"])
    else:
        out(f"before {result['before_ms']:.2f} ms, after {result['after_ms']:.2f} ms "
            f"({result['speedup']:.1f}x), same results: {result['same_results']}")
        out(("KEPT: " if result["kept"] else "NOT KEPT: ") + result["reason"]
            + (" (undone)" if result.get("undone") else ""))
    return answer, result


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--query", help="the query text")
    parser.add_argument("--query-file", help="file of queries separated by semicolons")
    parser.add_argument("--number", type=int, default=1, help="which query in the file (from 1)")
    parser.add_argument("--apply", action="store_true", help="apply the fix and time it")
    parser.add_argument("--min-speedup", type=float, default=MIN_SPEEDUP)
    parser.add_argument("--prompt-only", action="store_true", help="print the prompt and stop")
    parser.add_argument("--response", help="read the model's answer from this file instead of calling the API")
    args = parser.parse_args()

    run = make_runner()
    query = get_query(args)

    if args.prompt_only:
        plan = run("EXPLAIN (ANALYZE, BUFFERS) " + query)
        _, context = collect_context(run, query, plan)
        print(SYSTEM_PROMPT, "\n\n", build_prompt(query, plan, context), sep="")
        return

    llm = (lambda s, u: Path(args.response).read_text()) if args.response else anthropic_llm
    try:
        triage(run, query, llm, apply=args.apply, min_speedup=args.min_speedup)
    except (ValueError, RuntimeError) as error:
        sys.exit(f"error: {error}")


if __name__ == "__main__":
    main()
