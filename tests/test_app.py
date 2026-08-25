import os
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["APP_SECRET"] = "test-secret"
os.environ["FERNET_KEY"] = "x"  # not used by health

from app import app

def test_health():
    client = app.test_client()
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json["status"] == "ok"
