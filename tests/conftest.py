import os
import tempfile

# Must be set before app.db / app.triage.llm are imported: tests use throwaway databases.
_TMP = tempfile.mkdtemp()
os.environ["REPORTFIX_DB"] = os.path.join(_TMP, "test_reportfix.db")
os.environ["LLM_CACHE_DB"] = os.path.join(_TMP, "test_llm_cache.db")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import campus  # noqa: E402
from app.main import app  # noqa: E402
from app.triage import llm  # noqa: E402


@pytest.fixture(autouse=True)
def rules_only_chain(tmp_path):
    """Unit tests NEVER call a real AI: every test starts with an empty chain (rules mode).
    Tests that need an AI install a FakeProvider chain with `use_chain(...)`."""
    campus.set_active(None)  # every test starts with the real campus.json
    llm.set_chain(llm.Chain([], cache=llm.Cache(str(tmp_path / "cache.db"))))
    yield
    llm.set_chain(None)
    campus.set_active(None)


@pytest.fixture
def client():
    with TestClient(app) as c:
        c.post("/api/reset")
        yield c


@pytest.fixture
def ids(client):
    """name -> user id"""
    return {u["name"]: u["id"] for u in client.get("/api/users").json()}


def as_user(user_id):
    return {"X-User-Id": str(user_id)}
