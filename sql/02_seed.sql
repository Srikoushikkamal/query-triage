-- Deterministic fake data: 200k customers, 5k products, 2M orders, 6M order items.
-- Every order has exactly three items.

SELECT setseed(0.42);

INSERT INTO customers
SELECT i,
       'customer' || i || '@example.com',
       'Customer ' || i,
       (ARRAY['IE','GB','DE','FR','ES','IT','NL','PL'])[1 + (i % 8)],
       timestamptz '2022-01-01' + random() * interval '700 days'
FROM generate_series(1, 200000) AS i;

INSERT INTO products
SELECT i,
       'Product ' || i,
       (ARRAY['books','toys','garden','kitchen','sport','music','tools','pets','beauty','office'])[1 + (i % 10)],
       round((1 + random() * 199)::numeric, 2)
FROM generate_series(1, 5000) AS i;

INSERT INTO orders
SELECT i,
       1 + floor(random() * 200000)::int,
       timestamptz '2024-01-01' + random() * interval '730 days',
       (ARRAY['placed','paid','shipped','delivered'])[1 + floor(random() * 4)::int]
FROM generate_series(1, 2000000) AS i;

INSERT INTO order_items
SELECT i,
       1 + (i - 1) / 3,
       1 + floor(random() * 5000)::int,
       1 + floor(random() * 5)::int,
       round((1 + random() * 199)::numeric, 2)
FROM generate_series(1, 6000000) AS i;

-- This one index exists from the start, so Q2 has something to ignore.
CREATE INDEX orders_placed_at_idx ON orders (placed_at);

ANALYZE;
