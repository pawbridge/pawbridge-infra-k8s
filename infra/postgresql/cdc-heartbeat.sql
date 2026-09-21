-- Persistent CDC infrastructure, separate from application-owned business migrations.
-- Prerequisite: five schemas, restricted CDC roles, and five Outbox publications exist.
-- Apply with psql -v ON_ERROR_STOP=1 against the approved pawbridge database as its administrator.
-- Existing Outbox tables, business events, slots and offsets are never changed here.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
DO $heartbeat$
DECLARE
    service text;
    schema_name text;
    table_oid oid;
BEGIN
    IF current_database() <> 'pawbridge' THEN
        RAISE EXCEPTION 'Expected pawbridge database';
    END IF;
    FOREACH service IN ARRAY ARRAY['animal','user','community','store','payment'] LOOP
        schema_name := 'pawbridge_' || service;
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = schema_name || '_cdc' AND rolreplication AND NOT rolsuper)
           OR NOT EXISTS (SELECT 1 FROM pg_publication WHERE pubname = schema_name || '_outbox') THEN
            RAISE EXCEPTION 'Provision CDC role and publication first: %', service;
        END IF;
        EXECUTE format('CREATE TABLE IF NOT EXISTS %I.cdc_heartbeat (id smallint PRIMARY KEY CHECK (id = 1), touched_at timestamptz NOT NULL)', schema_name);
        table_oid := to_regclass(format('%I.cdc_heartbeat', schema_name));
        IF (SELECT count(*) FROM pg_attribute WHERE attrelid = table_oid AND attnum > 0 AND NOT attisdropped) <> 2
           OR NOT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = table_oid AND attname = 'id' AND atttypid = 'smallint'::regtype AND attnotnull)
           OR NOT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = table_oid AND attname = 'touched_at' AND atttypid = 'timestamptz'::regtype AND attnotnull) THEN
            RAISE EXCEPTION 'Unexpected heartbeat table contract: %', service;
        END IF;
        EXECUTE format('INSERT INTO %I.cdc_heartbeat VALUES (1,clock_timestamp()) ON CONFLICT (id) DO NOTHING', schema_name);
        EXECUTE format('GRANT SELECT ON %I.cdc_heartbeat TO %I', schema_name, schema_name || '_cdc');
        EXECUTE format('GRANT UPDATE (touched_at) ON %I.cdc_heartbeat TO %I', schema_name, schema_name || '_cdc');
        EXECUTE format('ALTER ROLE %I CONNECTION LIMIT 4', schema_name || '_cdc');
        IF NOT EXISTS (SELECT 1 FROM pg_publication_rel pr JOIN pg_publication p ON p.oid = pr.prpubid WHERE p.pubname = schema_name || '_outbox' AND pr.prrelid = table_oid) THEN
            EXECUTE format('ALTER PUBLICATION %I ADD TABLE %I.cdc_heartbeat', schema_name || '_outbox', schema_name);
        END IF;
    END LOOP;
END
$heartbeat$;
COMMIT;
