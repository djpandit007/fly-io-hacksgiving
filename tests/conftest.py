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
