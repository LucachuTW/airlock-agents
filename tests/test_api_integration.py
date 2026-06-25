"""Integration tests against the compose Postgres/Redis. Skipped when infra is down."""

import subprocess

import pytest
import sqlalchemy
from sqlalchemy import create_engine, text

from app.config import settings

SYNC_URL = settings.database_url.replace("+asyncpg", "+psycopg")


def _infra_up() -> bool:
    try:
        with create_engine(SYNC_URL, connect_args={"connect_timeout": 2}).connect():
            return True
    except sqlalchemy.exc.OperationalError:
        return False


pytestmark = pytest.mark.skipif(not _infra_up(), reason="postgres not running (make up)")


@pytest.fixture(scope="module")
def client():
    # Migrate + seed in a subprocess so the app's asyncpg pool stays on one event loop.
    subprocess.run(["uv", "run", "alembic", "upgrade", "head"], check=True)
    subprocess.run(["uv", "run", "python", "-m", "scripts.seed"], check=True)
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


def _login(client, email, password):
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_login_bad_credentials(client):
    r = client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": "nope"})
    assert r.status_code == 401


def test_role_filtered_tools(client):
    viewer = _login(client, "viewer@example.com", "viewer123")
    names = {t["name"] for t in client.get("/api/v1/tools", headers=viewer).json()}
    assert names == {"search_docs"}

    analyst = _login(client, "analyst@example.com", "analyst123")
    names = {t["name"] for t in client.get("/api/v1/tools", headers=analyst).json()}
    assert "create_ticket" in names and "run_query" in names


def test_admin_endpoints_forbidden_for_viewer(client):
    viewer = _login(client, "viewer@example.com", "viewer123")
    r = client.get("/api/v1/admin/audit", headers=viewer)
    assert r.status_code == 403


def test_grant_change_is_audited(client):
    admin = _login(client, "admin@example.com", "admin123")
    r = client.put(
        "/api/v1/admin/grants",
        json={"role": "viewer", "tool_name": "list_tables", "granted": True},
        headers=admin,
    )
    assert r.status_code == 200
    entries = client.get("/api/v1/admin/audit?action=admin.grant", headers=admin).json()
    assert entries and entries[0]["action"] == "admin.grant"
    assert entries[0]["detail"] == {"role": "viewer", "granted": True}
    assert entries[0]["resource"] == "list_tables"
    # revert
    client.put(
        "/api/v1/admin/grants",
        json={"role": "viewer", "tool_name": "list_tables", "granted": False},
        headers=admin,
    )


def test_audit_log_is_append_only():
    with create_engine(SYNC_URL).connect() as conn:
        with pytest.raises(sqlalchemy.exc.DatabaseError, match="append-only"):
            conn.execute(text("update audit_log set action = 'tampered'"))
