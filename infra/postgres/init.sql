-- Enables the pgvector extension for local development.
-- Runs automatically via docker-entrypoint-initdb.d on first container start.
-- No application tables are created here.
CREATE EXTENSION IF NOT EXISTS vector;
