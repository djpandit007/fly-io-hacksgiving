#!/usr/bin/env bash
# Create the clause-memory index once. The Sprites Pinecone connector cannot create indexes, so run
# this on your own machine with your Pinecone key (or create the same index in the Pinecone console:
# dense, 1024 dimensions, cosine, serverless aws/us-east-1, name "fine-print").
#   PINECONE_API_KEY=... ./scripts/setup_pinecone.sh
set -euo pipefail
INDEX="${PINECONE_INDEX:-fine-print}"
curl -sS -X POST https://api.pinecone.io/indexes \
  -H "Api-Key: ${PINECONE_API_KEY:?set PINECONE_API_KEY}" \
  -H "X-Pinecone-API-Version: 2025-04" -H "Content-Type: application/json" \
  -d "{\"name\": \"$INDEX\", \"dimension\": 1024, \"metric\": \"cosine\",
       \"spec\": {\"serverless\": {\"cloud\": \"aws\", \"region\": \"us-east-1\"}}}"
echo
