#!/usr/bin/env bash
# Create (or update) the two worker Sprites, install their runners, close their egress, take a clean
# base checkpoint of each, and save the checkpoint ids into .env.sprite:
#
#   fly-io-hacksgiving-scout     zero connectors; Python 3.12 + Playwright/Chromium  -> FP_SCOUT_BASE_CHECKPOINT
#   fly-io-hacksgiving-advocate  send-scoped AgentMail + OpenCode connectors;
#                                Python 3.12 + python-docx + LibreOffice + Playwright -> FP_ADVOCATE_BASE_CHECKPOINT
#
# With SPRITES_API_TOKEN set, it also creates the Gateway's prefix-scoped Sprites API connector (a Custom
# API connector that may only exec, set network policy and restore checkpoints on these two workers).
#
#   ./scripts/setup_swarm.sh             # both workers (and the connector if SPRITES_API_TOKEN is set)
#   ./scripts/setup_swarm.sh scout       # one worker
#   ./scripts/setup_swarm.sh advocate
#
# Then copy the connector's gateway base into .env.sprite as FP_SPRITES_API_BASE (./scripts/connectors.sh
# lists it) and redeploy the Gateway (./scripts/setup_sprite.sh).
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
  set_policy "$SCOUT" '[{"include": "defaults"}, {"action": "allow", "domain": "*.playwright.dev"}, {"action": "allow", "domain": "playwright.azureedge.net"}, {"action": "allow", "domain": "*.ubuntu.com"}, {"action": "allow", "domain": "astral.sh"}, {"action": "deny", "domain": "*"}]'
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
  set_policy "$ADVOCATE" '[{"include": "defaults"}, {"action": "allow", "domain": "*.playwright.dev"}, {"action": "allow", "domain": "playwright.azureedge.net"}, {"action": "allow", "domain": "*.ubuntu.com"}, {"action": "allow", "domain": "*.debian.org"}, {"action": "allow", "domain": "astral.sh"}, {"action": "deny", "domain": "*"}]'
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
  # Between jobs the Advocate reaches only the connector gateway (its send-scoped connectors).
  set_policy "$ADVOCATE" '[{"action": "allow", "domain": "api.sprites.dev"}, {"action": "deny", "domain": "*"}]'
  local id
  id="$(base_checkpoint "$ADVOCATE")"
  set_env FP_ADVOCATE_SPRITE "$ADVOCATE"
  set_env FP_ADVOCATE_BASE_CHECKPOINT "$id"
  echo "Advocate base checkpoint: $id"
}

# The Gateway's Sprites API connector: only the Gateway (label fineprint) may use it, and only for the
# three calls app/swarm.py makes, on the two workers.
create_sprites_connector() {
  local body
  body="$(umask 077; mktemp)"
  SCOUT="$SCOUT" ADVOCATE="$ADVOCATE" PREFIX="$PREFIX" python3 -I -c '
import json, os
s, a, p = os.environ["SCOUT"], os.environ["ADVOCATE"], os.environ["PREFIX"]
endpoints = [f"/v1/sprites/{n}/{tail}" for n in (s, a) for tail in ("exec", "policy/network", "checkpoints/*")]
print(json.dumps({
    "name": "sprites-swarm",
    "description": "Fine Print Gateway -> worker Sprites (exec, network policy, checkpoint restore)",
    "base_api_url": "https://api.sprites.dev",
    "auth_method": "header",
    "auth_header_prefix": "Bearer",
    "access_token": os.environ["SPRITES_API_TOKEN"],
    "access_policy": {"name_prefix": p, "sprite_labels": ["fineprint"], "allowed_endpoints": endpoints},
}))' > "$body"
  sprite api /v1/oauth/connections/custom_api -- -sS -X POST -H 'Content-Type: application/json' -d "@$body" >/dev/null
  rm -f "$body"
  echo "Created the sprites-swarm connector. Run ./scripts/connectors.sh and set FP_SPRITES_API_BASE."
}

[[ "$ONLY" == all || "$ONLY" == scout ]] && setup_scout
[[ "$ONLY" == all || "$ONLY" == advocate ]] && setup_advocate

if [[ "$ONLY" == all ]]; then
  if [[ -n "${SPRITES_API_TOKEN:-}" ]]; then
    create_sprites_connector
  else
    echo
    echo "Gateway connector: set SPRITES_API_TOKEN and re-run, or add a Custom API connector by hand"
    echo "(base https://api.sprites.dev, bearer token, sprite label fineprint, endpoints"
    echo " /v1/sprites/$SCOUT/{exec,policy/network,checkpoints/*} and the same for $ADVOCATE)."
  fi
fi

echo
echo "Next:"
echo "  1. Make sure no connector reaches the Scout: every existing connector should require sprite label"
echo "     'fineprint' (the Scout only has 'fineprint-scout'), not just the name prefix $PREFIX."
echo "  2. Give the Advocate its own AgentMail connector (send endpoint only) and OpenCode connector,"
echo "     each requiring sprite label 'fineprint-advocate'."
echo "  3. Set FP_SPRITES_API_BASE in .env.sprite and redeploy the Gateway: ./scripts/setup_sprite.sh"
