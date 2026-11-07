#!/bin/sh
# Only roles and privileges. Application tables and seed data belong to the app.
set -eu
psql -v ON_ERROR_STOP=1 --username postgres --dbname "$POSTGRES_DB" \
  --set=schema_password="$(cat /run/foxtrot/pg_schema)" \
  --set=writer_password="$(cat /run/foxtrot/pg_writer)" <<'SQL'
SELECT 'CREATE ROLE app_schema LOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app_schema') \gexec
SELECT 'CREATE ROLE app_writer NOLOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app_writer') \gexec
ALTER ROLE app_schema LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD :'schema_password';
ALTER ROLE app_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD :'writer_password';
SELECT format('GRANT CONNECT ON DATABASE %I TO app_schema, app_writer', current_database()) \gexec
GRANT USAGE, CREATE ON SCHEMA public TO app_schema;
GRANT USAGE ON SCHEMA public TO app_writer;
ALTER DEFAULT PRIVILEGES FOR ROLE app_schema IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_writer;
ALTER DEFAULT PRIVILEGES FOR ROLE app_schema IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO app_writer;
SQL
