-- Put the database back into its slow state (drops the indexes 99_fixes.sql adds).
DROP INDEX IF EXISTS customers_email_idx;
DROP INDEX IF EXISTS orders_customer_id_idx;
DROP INDEX IF EXISTS order_items_order_id_idx;
