import os
import sys
import psycopg2
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

# Force test database URLs in environment before any app modules are loaded
TEST_DB_NAME = "hamicloud_test"
TEST_DATABASE_URL = f"postgresql+asyncpg://hamicloud:hamicloud_secret@localhost:5432/{TEST_DB_NAME}"
TEST_DATABASE_URL_SYNC = f"postgresql://hamicloud:hamicloud_secret@localhost:5432/{TEST_DB_NAME}"

os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["DATABASE_URL_SYNC"] = TEST_DATABASE_URL_SYNC

# Ensure apps/api is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.config import settings
from app.db.session import engine
from app.main import app

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
ALEMBIC_INI = os.path.join(REPO_ROOT, "migrations", "alembic.ini")

TABLES_TO_TRUNCATE = [
    "audit_events",
    "secret_references",
    "idempotency_records",
    "quota_reservations",
    "consumed_events",
    "outbox_events",
    "execution_intents",
    "job_attempts",
    "jobs",
    "releases",
    "applications",
    "workspace_memberships",
    "workspaces",
]


@pytest.fixture(scope="session", autouse=True)
def setup_test_database():
    """Ensure hamicloud_test database exists and is migrated to head."""
    # 1. Connect to postgres database to ensure hamicloud_test exists
    admin_conn = psycopg2.connect("postgresql://hamicloud:hamicloud_secret@localhost:5432/postgres")
    admin_conn.autocommit = True
    cur = admin_conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB_NAME,))
    if not cur.fetchone():
        cur.execute(f"CREATE DATABASE {TEST_DB_NAME} OWNER hamicloud")
    cur.close()
    admin_conn.close()

    # 2. Run alembic upgrade head on hamicloud_test
    alembic_cfg = Config(ALEMBIC_INI)
    alembic_cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL_SYNC)
    command.upgrade(alembic_cfg, "head")

    # 3. Verify settings and engine are pointed at hamicloud_test, NEVER dev database
    assert "hamicloud_test" in settings.DATABASE_URL, "CRITICAL: DATABASE_URL not pointed to test db!"
    assert engine.url.database == TEST_DB_NAME, f"CRITICAL: Engine connected to {engine.url.database}, not {TEST_DB_NAME}!"


@pytest.fixture(autouse=True)
def clean_test_tables():
    """Truncate all tenant tables in hamicloud_test before and after each test."""
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    conn.autocommit = True
    cur = conn.cursor()
    truncate_sql = f"TRUNCATE TABLE {', '.join(TABLES_TO_TRUNCATE)} CASCADE;"
    cur.execute(truncate_sql)
    cur.close()
    conn.close()
    yield
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(truncate_sql)
    cur.close()
    conn.close()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
