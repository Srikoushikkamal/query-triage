-- A small shop: customers place orders, orders contain items, items are products.
-- Only primary keys exist here. The indexes that matter are added by 02_seed.sql
-- (orders.placed_at) or deliberately left out (the ones 99_fixes.sql creates).

DROP TABLE IF EXISTS order_items, orders, products, customers CASCADE;

CREATE TABLE customers (
    id          integer PRIMARY KEY,
    email       text        NOT NULL,
    name        text        NOT NULL,
    country     text        NOT NULL,
    created_at  timestamptz NOT NULL
);

CREATE TABLE products (
    id        integer PRIMARY KEY,
    name      text    NOT NULL,
    category  text    NOT NULL,
    price     numeric(8,2) NOT NULL
);

CREATE TABLE orders (
    id           integer PRIMARY KEY,
    customer_id  integer     NOT NULL REFERENCES customers (id),
    placed_at    timestamptz NOT NULL,
    status       text        NOT NULL
);

CREATE TABLE order_items (
    id          integer PRIMARY KEY,
    order_id    integer      NOT NULL REFERENCES orders (id),
    product_id  integer      NOT NULL REFERENCES products (id),
    quantity    integer      NOT NULL,
    unit_price  numeric(8,2) NOT NULL
);
