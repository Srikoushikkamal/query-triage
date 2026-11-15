-- Fixes for the three slow queries in 03_slow_queries.sql.

-- Q1: index the lookup column and the join column.
CREATE INDEX IF NOT EXISTS customers_email_idx    ON customers (email);
CREATE INDEX IF NOT EXISTS orders_customer_id_idx ON orders (customer_id);

-- Q3: index the foreign key on the big child table.
CREATE INDEX IF NOT EXISTS order_items_order_id_idx ON order_items (order_id);

-- Q2 is not fixed with an index. The obvious attempt,
--   CREATE INDEX ON orders ((placed_at::date));
-- is rejected because a timestamptz-to-date cast depends on the session time
-- zone and so is not IMMUTABLE. Rewrite the query instead, as a half-open
-- range on the raw indexed column:
--
--   SELECT count(*)
--   FROM orders
--   WHERE placed_at >= timestamptz '2025-01-15'
--     AND placed_at <  timestamptz '2025-01-16';
