-- Run as the dedicated demo database owner/admin; app_role must already exist.
-- psql "$DATABASE_ADMIN_URL" -v app_role=app -f bootstrap-grants.sql
-- No role/password creation and no data reset. DBNAME is psql's connected database.
\set ON_ERROR_STOP on
BEGIN;
GRANT CONNECT ON DATABASE :"DBNAME" TO :"app_role";
GRANT USAGE, CREATE ON SCHEMA public TO :"app_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"app_role";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"app_role";
COMMIT;
