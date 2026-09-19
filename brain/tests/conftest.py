import json
import os
import tempfile

import pytest

# isolated sqlite per test session, before app modules import settings
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["MEDIA_DIR"] = f"{_tmp}/media"
os.environ["BRAIN_TOKEN"] = "test-token"
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["OWNER_NAME"] = "Kishaunjith"

from app import llm as llm_mod  # noqa: E402
from app.db import init_db  # noqa: E402


class FakeLLM:
    """Scripted responses keyed by a substring of the NEW MESSAGE text."""

    def __init__(self):
        self.script: dict[str, dict] = {}
        self.calls: list[list[dict]] = []
        self.default = {"kind": "noise", "confidence": 0.9, "reason": "default"}
        self.raw = None  # set to a string to return it verbatim (e.g. a truncated answer)

    async def complete(self, messages, **kw):
        self.calls.append(messages)
        if self.raw is not None:
            return self.raw
        user = messages[-1]["content"]
        # match only the NEW MESSAGE part, not the chat context (which repeats earlier texts)
        user = user.split("NEW MESSAGE", 1)[-1]
        for key, resp in self.script.items():
            if key.lower() in user.lower():
                return "```json\n" + json.dumps(resp) + "\n```"  # fenced on purpose: parser must cope
        return json.dumps(self.default)


@pytest.fixture(autouse=True)
def fake_llm():
    fake = FakeLLM()
    llm_mod.LLM.override = fake  # type: ignore[assignment]
    yield fake
    llm_mod.LLM.override = None


@pytest.fixture(scope="session", autouse=True)
def _db():
    init_db()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        c.headers["Authorization"] = "Bearer test-token"
        yield c
