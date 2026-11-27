import os
import tempfile

# Settings are read at import time; give tests an isolated data dir and fake direct providers.
_tmp = tempfile.mkdtemp(prefix="fineprint-test-")
os.environ.update(
    {
        "FP_DATA_DIR": _tmp,
        "FP_SHARED_SECRET": "test-secret",
        "FP_CALENDAR_TOKEN": "cal-token",
        "FP_OPENCODE_BASE": "https://opencode.test/inference",
        "FP_OPENCODE_KEY": "oc-key",
        "FP_PINECONE_BASE": "https://pinecone.test",
        "FP_PINECONE_KEY": "pc-key",
    }
)
os.environ.setdefault("FP_OWNER_CLAIM_CODE", "owner-code")
os.environ.setdefault("FP_JOIN_CODE", "join-code")
os.environ.setdefault("FP_EXA_BASE", "https://exa.test")
os.environ.setdefault("FP_AGENTMAIL_BASE", "https://agentmail.test")
os.environ.setdefault("FP_AGENTMAIL_INBOX_ID", "inbox_1")
os.environ.setdefault("FP_AGENTMAIL_ADDRESS", "fineprint@agentmail.to")
os.environ.setdefault("FP_AGENTMAIL_WEBHOOK_SECRET", "whsec_dGVzdC1zZWNyZXQta2V5LWZvci1zdml4")  # base64("test-secret-key-for-svix")


import pytest
from pydantic_ai import models

# No test may reach a real model; tests that exercise the HTTP path opt in with a `with` block.
models.ALLOW_MODEL_REQUESTS = False


@pytest.fixture(autouse=True)
def _fresh_overview_cache():
    """Overviews are cached per process; each test starts without another test's overview."""
    from app import agent

    agent.OVERVIEW_CACHE.clear()
    yield
    agent.OVERVIEW_CACHE.clear()


@pytest.fixture(autouse=True)
def _scout_crawls_the_url_as_given(monkeypatch):
    """No test follows real redirects before a Scout crawl; tests/test_swarm.py tests landing_url itself."""
    from app import swarm

    async def same(url, client=None):
        return url

    monkeypatch.setattr(swarm, "landing_url", same)
    monkeypatch.setattr(swarm, "POLICY_SETTLE", 0)  # the live settle pause; tests/test_swarm.py checks it
