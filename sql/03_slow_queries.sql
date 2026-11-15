-- Three queries, each slow for a different reason.
-- Pick one customer and one order that exist in the seed data.

-- Q1: find one customer's orders by email.
-- No index on customers.email, none on orders.customer_id.
SELECT o.id, o.placed_at, o.status
FROM customers c
JOIN orders o ON o.customer_id = c.id
WHERE c.email = 'customer123456@example.com'
ORDER BY o.placed_at DESC;

-- Q2: one day of orders, filtered through a function.
-- orders.placed_at is indexed, but DATE(placed_at) hides it from the planner.
SELECT count(*)
FROM orders
WHERE DATE(placed_at) = DATE '2025-01-15';

-- Q3: one order's basket, by product category.
-- No index on order_items.order_id, so six million rows get scanned for three.
SELECT p.category, sum(oi.quantity * oi.unit_price) AS spend
FROM order_items oi
JOIN products p ON p.id = oi.product_id
WHERE oi.order_id = 1234567
GROUP BY p.category;
