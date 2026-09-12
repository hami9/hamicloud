-- Initialize HamiCloud PostgreSQL extensions and baseline schema
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Log database initialization
DO $$
BEGIN
    RAISE NOTICE 'HamiCloud PostgreSQL database initialized successfully.';
END $$;
