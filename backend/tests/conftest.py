import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DATA_MODE", "demo")
    monkeypatch.delenv("EDGE_CONFIG", raising=False)
    main._cache.clear()
    return TestClient(main.app)
