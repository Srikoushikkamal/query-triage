-- Put the database back into its slow state: drops the indexes 99_fixes.sql adds,
-- and any index triage.py created (its default names start with qt_).
DROP INDEX IF EXISTS customers_email_idx;
DROP INDEX IF EXISTS orders_customer_id_idx;
DROP INDEX IF EXISTS order_items_order_id_idx;

DO $$
DECLARE r record;
BEGIN
    FOR r IN SELECT indexname FROM pg_indexes
             WHERE schemaname = 'public' AND indexname LIKE 'qt\_%'
    LOOP
        EXECUTE format('DROP INDEX %I', r.indexname);
    END LOOP;
END $$;
