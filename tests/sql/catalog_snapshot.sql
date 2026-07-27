-- Normalised catalog snapshot of the pyjobby schema in the `public` namespace.
-- One text line per catalog object, ordered, so two databases can be compared
-- as sorted text. Used by tests/test_migrations.py to prove that an UPGRADED
-- database is indistinguishable from a FRESH install.
--
-- It reads the CATALOG and not a version number, because a version number is
-- exactly what a stale database lies about: the whole failure this exists to
-- prevent was a database that recorded no pending migrations while missing
-- three columns, two functions and ten indexes. Everything that can differ
-- between the two install paths is therefore in here -- column types,
-- nullability and defaults, index definitions (including their predicates,
-- so a partial index cannot pass as a plain one), function BODIES by md5,
-- trigger definitions (including WHEN clauses and DEFERRABLE), constraints,
-- view definitions, enum labels in order, and per-table storage parameters.
--
-- Comments are deliberately absent: they are documentation, and requiring a
-- new migration file every time a COMMENT's prose is edited would make this
-- check something people route around.
--
-- test_schema_fingerprint is the test harness's own bookkeeping table (see
-- tests/conftest.py), never part of the schema, so it is filtered out to
-- allow snapshotting the session database as well as a scratch one.
SELECT line FROM (
    SELECT format('column %s.%s %s null=%s default=%s',
                  c.table_name, c.column_name, c.data_type,
                  c.is_nullable, coalesce(c.column_default, '-')) AS line
      FROM information_schema.columns c
      JOIN information_schema.tables t
        ON t.table_schema = c.table_schema AND t.table_name = c.table_name
     WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'
    UNION ALL
    SELECT format('index %s', indexdef) FROM pg_indexes WHERE schemaname = 'public'
    UNION ALL
    SELECT format('view %s %s', viewname, definition) FROM pg_views WHERE schemaname = 'public'
    UNION ALL
    SELECT format('function %s(%s) %s', p.proname,
                  pg_get_function_identity_arguments(p.oid), md5(p.prosrc))
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public'
    UNION ALL
    SELECT format('trigger %s', pg_get_triggerdef(tg.oid))
      FROM pg_trigger tg JOIN pg_class cl ON cl.oid = tg.tgrelid
      JOIN pg_namespace n ON n.oid = cl.relnamespace
     WHERE n.nspname = 'public' AND NOT tg.tgisinternal
    UNION ALL
    SELECT format('constraint %s.%s %s', cl.relname, co.conname, pg_get_constraintdef(co.oid))
      FROM pg_constraint co JOIN pg_class cl ON cl.oid = co.conrelid
      JOIN pg_namespace n ON n.oid = cl.relnamespace
     WHERE n.nspname = 'public'
    UNION ALL
    SELECT format('enum %s = %s', t.typname,
                  (SELECT string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder)
                     FROM pg_enum e WHERE e.enumtypid = t.oid))
      FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
     WHERE n.nspname = 'public' AND t.typtype = 'e'
    UNION ALL
    SELECT format('reloptions %s %s', cl.relname, array_to_string(cl.reloptions, ','))
      FROM pg_class cl JOIN pg_namespace n ON n.oid = cl.relnamespace
     WHERE n.nspname = 'public' AND cl.relkind = 'r' AND cl.reloptions IS NOT NULL
) s
WHERE line NOT LIKE '%test_schema_fingerprint%'
ORDER BY line;
