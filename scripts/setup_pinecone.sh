#!/usr/bin/env bash
# Create the clause-memory index with integrated embedding, then print its data-plane host.
#   PINECONE_API_KEY=... ./scripts/setup_pinecone.sh
set -euo pipefail
INDEX="${PINECONE_INDEX:-fine-print}"
H=(-H "Api-Key: ${PINECONE_API_KEY:?set PINECONE_API_KEY}" -H "X-Pinecone-API-Version: 2025-04" -H "Content-Type: application/json")

curl -sS "${H[@]}" -X POST https://api.pinecone.io/indexes/create-for-model -d @- <<JSON || true
{"name": "$INDEX", "cloud": "aws", "region": "us-east-1",
 "embed": {"model": "llama-text-embed-v2", "field_map": {"text": "chunk_text"}}}
JSON
echo
echo "Index host (use https://<host> as the Pinecone connector or FP_PINECONE_BASE):"
curl -sS "${H[@]}" "https://api.pinecone.io/indexes/$INDEX" | python3 -c 'import json,sys; print(json.load(sys.stdin)["host"])'
