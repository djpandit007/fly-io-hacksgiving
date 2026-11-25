#!/usr/bin/env bash
# Create (or update) the Fine Print Sprite, copy the code and .env.sprite onto it, and run the app
# as a Sprite service on port 8080 behind a public URL.
#
#   ./scripts/setup_sprite.sh            # first deploy and every redeploy
#
# Requires: the `sprite` CLI, logged in (`curl -fsSL https://sprites.dev/install.sh | sh && sprite login`)
# and a filled-in .env.sprite (copy .env.example).
set -euo pipefail

NAME="${SPRITE_NAME:-fine-print}"
REMOTE_DIR="/home/sprite/fine-print"
cd "$(dirname "$0")/.."

[[ -f .env.sprite ]] || { echo "Create .env.sprite from .env.example first." >&2; exit 1; }

if ! sprite list 2>/dev/null | grep -qw "$NAME"; then
  echo "Creating Sprite $NAME"
  sprite create "$NAME" --label fineprint
fi
sprite use "$NAME"

echo "Copying code"
tmp="$(mktemp -d)"
git ls-files -z --cached --others --exclude-standard | tar --null -T - -czf "$tmp/app.tgz"
sprite file push "$tmp/app.tgz" /tmp/fine-print.tgz
sprite file push .env.sprite /tmp/fine-print.env
rm -rf "$tmp"

sprite exec -- bash -lc "
  set -euo pipefail
  mkdir -p $REMOTE_DIR
  tar -xzf /tmp/fine-print.tgz -C $REMOTE_DIR
  install -m 600 /tmp/fine-print.env $REMOTE_DIR/.env
  rm -f /tmp/fine-print.tgz /tmp/fine-print.env
  command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
  cd $REMOTE_DIR && \$(command -v uv || echo ~/.local/bin/uv) sync --no-dev
"

UV="$(sprite exec -- bash -lc 'command -v uv || echo ~/.local/bin/uv' | tr -d '\r')"
if sprite exec -- sprite-env services get fineprint >/dev/null 2>&1; then
  echo "Restarting service"
  sprite exec -- sprite-env services restart fineprint
else
  echo "Creating service"
  sprite exec -- sprite-env services create fineprint \
    --cmd "$UV" \
    --args "run,--no-dev,uvicorn,app.main:app,--host,0.0.0.0,--port,8080" \
    --dir "$REMOTE_DIR" \
    --http-port 8080
fi

# Telegram and the daily cron must reach the Sprite; every route but /health checks a secret.
sprite url update --auth public
sprite info
echo
echo "Next: set FP_PUBLIC_URL in .env.sprite to the URL above, redeploy, then:"
echo "  curl -X POST \"\$FP_PUBLIC_URL/admin/telegram-webhook\" -H \"X-Fine-Print-Secret: \$FP_SHARED_SECRET\""
