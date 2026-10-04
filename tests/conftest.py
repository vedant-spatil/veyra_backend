import os
from urllib.parse import urlparse

import psycopg
import pytest
from fastapi.testclient import TestClient

ADMIN_CANDIDATES = [
    os.environ.get("VEYRA_ADMIN_DSN", ""),
    "postgresql://veyra:veyra@127.0.0.1:5432/veyra",
    "postgresql://veyra:veyra@127.0.0.1:5432/postgres",
    "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
]
TEST_DB = f"veyra_test_{os.getpid()}"


def _admin_dsn() -> str:
    last_error = None
    for dsn in ADMIN_CANDIDATES:
        if not dsn:
            continue
        try:
            with psycopg.connect(dsn, connect_timeout=3) as conn:
                conn.execute("select 1")
            return dsn
        except Exception as exc:
            last_error = exc
    raise RuntimeError(last_error or "postgres is not reachable")


def _prepare_database() -> str:
    admin = _admin_dsn()
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{TEST_DB}"')
    return admin


try:
    ADMIN_DSN = _prepare_database()
except Exception as exc:
    ADMIN_DSN = ""
    POSTGRES_ERROR = str(exc)
else:
    POSTGRES_ERROR = ""

def _database_url(admin_dsn: str) -> str:
    if not admin_dsn:
        return f"postgresql+psycopg://veyra:veyra@127.0.0.1:5432/{TEST_DB}"
    parsed = urlparse(admin_dsn)
    auth = parsed.username or "veyra"
    if parsed.password:
        auth += f":{parsed.password}"
    port = f":{parsed.port}" if parsed.port else ""
    return f"postgresql+psycopg://{auth}@{parsed.hostname}{port}/{TEST_DB}"


os.environ["DATABASE_URL"] = _database_url(ADMIN_DSN)
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["ALLOWED_EMAILS"] = "allowed@example.com"
os.environ["GOOGLE_CLIENT_ID"] = "test-client"
os.environ["GOOGLE_CLIENT_SECRET"] = "test-secret"
os.environ["PUBLIC_BASE_URL"] = "https://voice.example.com"
os.environ["FRONTEND_ORIGINS"] = "http://127.0.0.1:5173"
os.environ["PAYU_ENV"] = "test"
os.environ["PAYU_KEY"] = "merchant-key"
os.environ["PAYU_SALT"] = "merchant-salt"
os.environ["REDIS_URL"] = "redis://127.0.0.1:6379/15"


@pytest.fixture()
def client():
    if not ADMIN_DSN:
        pytest.skip(f"throwaway Postgres is unavailable: {POSTGRES_ERROR}")
    import app.db as dbmod
    import app.models  # noqa: F401
    from app.main import app
    from app.settings import get_settings

    get_settings.cache_clear()
    dbmod.engine = dbmod.make_engine()
    dbmod.SessionLocal.configure(bind=dbmod.engine)
    dbmod.Base.metadata.drop_all(dbmod.engine)
    dbmod.Base.metadata.create_all(dbmod.engine)
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client
    dbmod.Base.metadata.drop_all(dbmod.engine)


def pytest_sessionfinish(session, exitstatus):
    if not ADMIN_DSN:
        return
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
