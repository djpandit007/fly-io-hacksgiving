import os
import tempfile

# Settings are read at import time; give tests an isolated data dir and fake direct providers.
_tmp = tempfile.mkdtemp(prefix="fineprint-test-")
os.environ.update(
    {
        "FP_DATA_DIR": _tmp,
        "FP_SHARED_SECRET": "test-secret",
        "FP_CALENDAR_TOKEN": "cal-token",
        "FP_TYPESAFE_BASE": "https://typesafe.test",
        "FP_TYPESAFE_KEY": "ts-key",
        "FP_OLLAMA_BASE": "https://ollama.test",
        "FP_OLLAMA_KEY": "ol-key",
        "FP_PINECONE_BASE": "https://pinecone.test",
        "FP_PINECONE_KEY": "pc-key",
    }
)
