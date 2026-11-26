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


@pytest.fixture(autouse=True)
def _fresh_overview_cache():
    """Overviews are cached per process; each test starts without another test's overview."""
    from app import agent

    agent.OVERVIEW_CACHE.clear()
    yield
    agent.OVERVIEW_CACHE.clear()
