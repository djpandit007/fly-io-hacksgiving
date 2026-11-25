#!/usr/bin/env bash
# Show the connectors this Sprite may use; copy each gateway base into .env.sprite as FP_<NAME>_BASE.
set -euo pipefail
sprite exec -- curl -s https://api.sprites.dev/v1/gateway/list
