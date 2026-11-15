# query-triage

A tool that reads a slow Postgres query's execution plan, asks an LLM what is
wrong with it, and proves the suggested fix by re-timing the query.

This repository is at step one: a Postgres database with eight million rows and
three queries that are genuinely slow, each for a different reason. The tool
itself comes next.

## Why

Reading `EXPLAIN ANALYZE` output is a skill most people never acquire, and the
output is dense enough that an LLM is genuinely useful at it. The interesting
part is not the suggestion, it is the verification: the tool applies its own
fix and measures whether the query actually got faster.

## Running it

```bash
docker compose up -d

docker compose exec -T db psql -U postgres -d shop -f /sql/01_schema.sql
docker compose exec -T db psql -U postgres -d shop -f /sql/02_seed.sql   # about 80 seconds
```

That gives you:

| table         | rows      |
|---------------|-----------|
| `customers`   | 200,000   |
| `products`    | 5,000     |
| `orders`      | 2,000,000 |
| `order_items` | 6,000,000 |

Time the queries, fix them and time them again in one go:

```bash
PSQL="docker compose exec -T db psql -U postgres -d shop" python bench.py
```

Or run a single query by hand:

```bash
docker compose exec -T db psql -U postgres -d shop \
  -c "EXPLAIN (ANALYZE, BUFFERS) <query from sql/03_slow_queries.sql>"
```

`sql/99_fixes.sql` applies the fixes and `sql/00_reset.sql` puts the database
back into its slow state. `bench.py` does both for you.

## The three queries

Each timing is the median of seven `EXPLAIN (ANALYZE)` runs after one warm-up.
They were measured on PostgreSQL 16.15 installed directly on a 2-vCPU, 8 GB
Linux machine with `shared_buffers` at 1 GB and a warm cache. Run `bench.py`
and expect different absolute numbers on your machine; the ratios are what
matter.

| | before | after | speed-up |
|---|---|---|---|
| Q1 | 142.69 ms | 0.23 ms | 631x |
| Q2 | 134.64 ms | 0.48 ms | 278x |
| Q3 | 172.87 ms | 0.17 ms | 1041x |

### Q1: no index on the join column

Finding one customer's orders by email. There is no index on `customers.email`
and none on `orders.customer_id`, so the plan is a parallel sequential scan of
`customers` to find the email and another of all two million `orders` to find
its 13 orders.

Fix: `CREATE INDEX ON customers (email)` and `CREATE INDEX ON orders (customer_id)`.
The plan becomes an index scan on the email and a bitmap index scan on the orders.

### Q2: a function defeating an existing index

`orders.placed_at` is indexed, but the query filters on `DATE(placed_at)`. The
planner cannot match a function result against an index on the raw column, so
the index sits unused and the plan is a parallel sequential scan.

The obvious fix does not work. `CREATE INDEX ON orders ((placed_at::date))` is
rejected:

```
ERROR:  functions in index expression must be marked IMMUTABLE
```

Casting `timestamptz` to `date` depends on the session timezone, so the result
is not stable enough to index. The query has to change instead, to a half-open
range on the raw column:

```sql
WHERE placed_at >= timestamptz '2025-01-15'
  AND placed_at <  timestamptz '2025-01-16'
```

Both forms return the same count (2,775 orders for that day). This is the most
interesting of the three, because the answer is not an index.

### Q3: no index on a large child table's foreign key

Pulling one order's basket by category. `order_items.order_id` is unindexed, so
finding three rows means scanning six million.

Fix: `CREATE INDEX ON order_items (order_id)`.

## Next

1. `triage.py`: connect, run `EXPLAIN (ANALYZE, BUFFERS)`, collect the schema
   and row counts for the tables involved, send all of it to an LLM, print the
   diagnosis.
2. `--apply`: create the suggested index, re-run, print both timings.
3. Tests and a GitHub Actions workflow.
