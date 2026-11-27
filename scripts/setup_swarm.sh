#!/usr/bin/env bash
# Create (or update) the two worker Sprites, install their runners, close their egress, take a clean
# base checkpoint of each, and save the checkpoint ids into .env.sprite:
#
#   fly-io-hacksgiving-scout     zero connectors; Python 3.12 + Playwright/Chromium  -> FP_SCOUT_BASE_CHECKPOINT
#   fly-io-hacksgiving-advocate  send-scoped AgentMail + OpenCode connectors;
#                                Python 3.12 + python-docx + LibreOffice + Playwright -> FP_ADVOCATE_BASE_CHECKPOINT
#
#   ./scripts/setup_swarm.sh             # both workers
#   ./scripts/setup_swarm.sh scout       # one worker
#   ./scripts/setup_swarm.sh advocate
#
# Set ADVOCATE_AGENTMAIL_BASE, ADVOCATE_AGENTMAIL_INBOX_ID and ADVOCATE_OPENCODE_BASE (the Advocate's own
# connectors) to write its runner config. Then set FP_SPRITES_API_BASE=https://api.sprites.dev and
# FP_SPRITES_TOKEN in .env.sprite and redeploy the Gateway (./scripts/setup_sprite.sh). The Gateway needs
# the token itself: the connector gateway refuses Custom API connectors that target the Sprites API.
#
# Requires: the `sprite` CLI, logged in, python3 locally, and a filled-in .env.sprite.
set -euo pipefail

PREFIX="${SPRITE_NAME:-fly-io-hacksgiving}"
SCOUT="$PREFIX-scout"
ADVOCATE="$PREFIX-advocate"
ONLY="${1:-all}"
WORKER_DIR="/home/sprite/fine-print-worker"   # must match app/swarm.py WORKER_DIR
cd "$(dirname "$0")/.."

[[ -f .env.sprite ]] || { echo "Create .env.sprite from .env.example first." >&2; exit 1; }
case "$ONLY" in all|scout|advocate) ;; *) echo "usage: $0 [scout|advocate]" >&2; exit 2 ;; esac

# Set KEY=value in .env.sprite, replacing an existing line.
set_env() {
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  grep -v "^$key=" .env.sprite > "$tmp" || true
  printf '%s=%s\n' "$key" "$value" >> "$tmp"
  cat "$tmp" > .env.sprite
  rm -f "$tmp"
}

# POST a JSON network policy to a worker (the CLI has no policy command; `sprite api` calls the API).
set_policy() {
  local name="$1" rules="$2"
  sprite api "/v1/sprites/$name/policy/network" -- -sS -X POST \
    -H 'Content-Type: application/json' -d "{\"rules\": $rules}" >/dev/null
}

ensure_sprite() {
  local name="$1" label="$2"
  if ! sprite list 2>/dev/null | grep -qw "$name"; then
    echo "Creating Sprite $name"
    sprite create --skip-console --label "$label" "$name"
  fi
}

# Copy workers/ onto the Sprite and build its virtualenv with uv-managed Python 3.12.
push_runners() {
  local name="$1" packages="$2" tmp
  tmp="$(mktemp -d)"
  COPYFILE_DISABLE=1 tar -czf "$tmp/workers.tgz" workers
  sprite file push -s "$name" "$tmp/workers.tgz" /home/sprite/.fine-print-workers.tgz
  rm -rf "$tmp"
  sprite exec -s "$name" --no-stdin -- bash -lc "
    set -euo pipefail
    rm -rf $WORKER_DIR/workers && mkdir -p $WORKER_DIR
    tar -xzf /home/sprite/.fine-print-workers.tgz -C $WORKER_DIR
    rm -f /home/sprite/.fine-print-workers.tgz
    command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
    UV=\$(command -v uv || echo ~/.local/bin/uv)
    cd $WORKER_DIR
    [[ -x .venv/bin/python ]] || UV_PYTHON_PREFERENCE=only-managed \$UV venv --python 3.12 .venv
    UV_PYTHON_PREFERENCE=only-managed \$UV pip install --python .venv/bin/python $packages
    .venv/bin/python -m playwright install --with-deps chromium
  "
}

# Prove the runner starts and answers in the result envelope (no network needed: a bad job).
smoke_test() {
  local name="$1" runner="$2"
  printf '{}' | sprite exec -s "$name" --stdin -- "$WORKER_DIR/.venv/bin/python" "$WORKER_DIR/workers/$runner" \
    | grep -q '<<FP-RESULT>>' || { echo "$runner did not answer on $name" >&2; exit 1; }
}

# Take a base checkpoint and print its id (newest checkpoint carrying our comment).
base_checkpoint() {
  local name="$1" comment="fine-print base $(date -u +%Y%m%dT%H%M%SZ)"
  sprite checkpoint create -s "$name" --comment "$comment" >/dev/null
  sprite api "/v1/sprites/$name/checkpoints" -- -sS | COMMENT="$comment" python3 -I -c '
import json, os, sys
mine = [c for c in json.load(sys.stdin) if c.get("comment") == os.environ["COMMENT"]]
if not mine:
    sys.exit("checkpoint not found")
print(sorted(mine, key=lambda c: c["create_time"])[-1]["id"])
'
}

setup_scout() {
  echo "== Scout: $SCOUT (no connectors)"
  ensure_sprite "$SCOUT" fineprint-scout
  set_policy "$SCOUT" '[{"include": "defaults"}, {"action": "allow", "domain": "*.playwright.dev"}, {"action": "allow", "domain": "playwright.azureedge.net"}, {"action": "allow", "domain": "playwright.download.prss.microsoft.com"}, {"action": "allow", "domain": "*.ubuntu.com"}, {"action": "allow", "domain": "astral.sh"}, {"action": "allow", "domain": "github.com"}, {"action": "allow", "domain": "*.githubusercontent.com"}, {"action": "allow", "domain": "pypi.org"}, {"action": "allow", "domain": "files.pythonhosted.org"}, {"action": "deny", "domain": "*"}]'
  push_runners "$SCOUT" "playwright"
  smoke_test "$SCOUT" scout_runner.py
  # Idle with no egress at all; each job opens only its target's site.
  set_policy "$SCOUT" '[{"action": "deny", "domain": "*"}]'
  local id
  id="$(base_checkpoint "$SCOUT")"
  set_env FP_SCOUT_SPRITE "$SCOUT"
  set_env FP_SCOUT_BASE_CHECKPOINT "$id"
  echo "Scout base checkpoint: $id"
}

setup_advocate() {
  echo "== Advocate: $ADVOCATE (send-scoped AgentMail + OpenCode connectors)"
  ensure_sprite "$ADVOCATE" fineprint-advocate
  set_policy "$ADVOCATE" '[{"include": "defaults"}, {"action": "allow", "domain": "*.playwright.dev"}, {"action": "allow", "domain": "playwright.azureedge.net"}, {"action": "allow", "domain": "playwright.download.prss.microsoft.com"}, {"action": "allow", "domain": "*.ubuntu.com"}, {"action": "allow", "domain": "*.debian.org"}, {"action": "allow", "domain": "astral.sh"}, {"action": "allow", "domain": "github.com"}, {"action": "allow", "domain": "*.githubusercontent.com"}, {"action": "allow", "domain": "pypi.org"}, {"action": "allow", "domain": "files.pythonhosted.org"}, {"action": "deny", "domain": "*"}]'
  sprite exec -s "$ADVOCATE" --no-stdin -- bash -lc "
    set -euo pipefail
    command -v soffice >/dev/null || {
      sudo apt-get update -qq
      sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq libreoffice-writer-nogui \
        || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq libreoffice-writer
    }
  "
  push_runners "$ADVOCATE" "playwright python-docx"
  if sprite exec -s "$ADVOCATE" --no-stdin -- test -f "$WORKER_DIR/workers/advocate_runner.py"; then
    smoke_test "$ADVOCATE" advocate_runner.py
  else
    echo "(workers/advocate_runner.py not present yet; skipping its smoke test)"
  fi
  # The runner's connector gateway URLs (the Advocate's own send-scoped AgentMail and OpenCode
  # connectors; the gateway attaches credentials, so no keys). Baked into the base checkpoint.
  if [[ -n "${ADVOCATE_AGENTMAIL_BASE:-}" || -n "${ADVOCATE_OPENCODE_BASE:-}" ]]; then
    printf 'FP_AGENTMAIL_BASE=%s\nFP_AGENTMAIL_INBOX_ID=%s\nFP_OPENCODE_BASE=%s\n' \
      "${ADVOCATE_AGENTMAIL_BASE:-}" "${ADVOCATE_AGENTMAIL_INBOX_ID:-}" "${ADVOCATE_OPENCODE_BASE:-}" \
      | sprite exec -s "$ADVOCATE" --stdin -- bash -lc "cat > $WORKER_DIR/advocate.env"
  else
    echo "(set ADVOCATE_AGENTMAIL_BASE, ADVOCATE_AGENTMAIL_INBOX_ID and ADVOCATE_OPENCODE_BASE to configure the runner)"
  fi
  # Between jobs the Advocate reaches only the connector gateway (its send-scoped connectors).
  set_policy "$ADVOCATE" '[{"action": "allow", "domain": "api.sprites.dev"}, {"action": "deny", "domain": "*"}]'
  local id
  id="$(base_checkpoint "$ADVOCATE")"
  set_env FP_ADVOCATE_SPRITE "$ADVOCATE"
  set_env FP_ADVOCATE_BASE_CHECKPOINT "$id"
  echo "Advocate base checkpoint: $id"
}

[[ "$ONLY" == all || "$ONLY" == scout ]] && setup_scout
[[ "$ONLY" == all || "$ONLY" == advocate ]] && setup_advocate

echo
echo "Next:"
echo "  1. Make sure no connector reaches the Scout: every existing connector should require sprite label"
echo "     'fineprint' (the Scout only has 'fineprint-scout'), not just the name prefix $PREFIX."
echo "  2. Give the Advocate its own AgentMail connector (send endpoint only) and OpenCode connector,"
echo "     each requiring sprite label 'fineprint-advocate'."
echo "  3. Set FP_SPRITES_API_BASE=https://api.sprites.dev and FP_SPRITES_TOKEN in .env.sprite, then"
echo "     redeploy the Gateway: ./scripts/setup_sprite.sh"
