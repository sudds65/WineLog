"""Point the app at a throwaway database before any app module is imported."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = tempfile.mkdtemp(prefix="winelog-tests-")
os.environ["WINELOG_DATA_DIR"] = _TMP
os.environ["WINELOG_DB"] = str(Path(_TMP) / "test.db")
os.environ["WINELOG_UPLOAD_DIR"] = str(Path(_TMP) / "receipts")

import pytest  # noqa: E402

from app import auth, db  # noqa: E402

TEST_USER = "tester"
TEST_PASSWORD = "correct-horse-battery"


@pytest.fixture()
def fresh_db():
    """A clean database for each test."""
    path = Path(os.environ["WINELOG_DB"])
    path.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    db.init_db()
    with db.get_conn() as conn:
        with db.transaction(conn):
            auth.create_user(conn, TEST_USER, TEST_PASSWORD)
    yield path


@pytest.fixture()
def client(fresh_db):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        test_client.headers.update({"X-WineLog-App": "1"})
        yield test_client


@pytest.fixture()
def auth_client(client):
    response = client.post(
        "/api/login", json={"username": TEST_USER, "password": TEST_PASSWORD}
    )
    assert response.status_code == 200
    return client
