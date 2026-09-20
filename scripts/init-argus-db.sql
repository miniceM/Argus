-- scripts/init-argus-db.sql
-- Initialize dedicated Argus database and role within PostgreSQL instance

SELECT 'CREATE USER argus WITH PASSWORD ''arguspassword'''
WHERE NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'argus')\gexec

SELECT 'CREATE DATABASE argus OWNER argus'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'argus')\gexec

GRANT ALL PRIVILEGES ON DATABASE argus TO argus;

\connect argus

GRANT ALL ON SCHEMA public TO argus;
ALTER SCHEMA public OWNER TO argus;
