#!/usr/bin/env bash
# Create (or update) the Fine Print Sprite, copy the code and .env.sprite onto it, and run the app
# as a Sprite service on port 8080 behind a public URL.
#
#   ./scripts/setup_sprite.sh            # deploy or redeploy; URL stays private
#   ./scripts/setup_sprite.sh --public   # also make the URL public (needed for Telegram and the cron)
#
# Requires: the `sprite` CLI, logged in (`curl -fsSL https://sprites.dev/install.sh | sh && sprite login`)
# and a filled-in .env.sprite (copy .env.example).
set -euo pipefail

NAME="${SPRITE_NAME:-fly-io-hacksgiving}"
PUBLIC="${1:-}"
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
git ls-files -z --cached --others --exclude-standard | COPYFILE_DISABLE=1 tar --null -T - -czf "$tmp/app.tgz"
sprite file push "$tmp/app.tgz" /home/sprite/.fine-print-upload.tgz
sprite file push .env.sprite /home/sprite/.fine-print-upload.env
rm -rf "$tmp"

sprite exec --no-stdin -- bash -lc "
  set -euo pipefail
  mkdir -p $REMOTE_DIR
  tar -xzf /home/sprite/.fine-print-upload.tgz -C $REMOTE_DIR
  install -m 600 /home/sprite/.fine-print-upload.env $REMOTE_DIR/.env
  rm -f /home/sprite/.fine-print-upload.tgz /home/sprite/.fine-print-upload.env
  command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv-managed Python 3.12: the Sprite's pyenv shims can hang uv's interpreter discovery.
  cd $REMOTE_DIR && UV_PYTHON_PREFERENCE=only-managed \$(command -v uv || echo ~/.local/bin/uv) sync --no-dev --python 3.12
"

UV="$(sprite exec --no-stdin -- bash -lc 'command -v uv || echo ~/.local/bin/uv' | tr -d '\r')"
if sprite exec --no-stdin -- sprite-env services get fineprint >/dev/null 2>&1; then
  echo "Restarting service"
  sprite exec --no-stdin -- sprite-env services restart fineprint
else
  echo "Creating service"
  sprite exec --no-stdin -- sprite-env services create fineprint \
    --cmd "$UV" \
    --args "run,--no-dev,--no-sync,uvicorn,app.main:app,--host,0.0.0.0,--port,8080" \
    --env "UV_PYTHON_PREFERENCE=only-managed" \
    --dir "$REMOTE_DIR" \
    --http-port 8080
fi

# Telegram and the daily cron must reach the Sprite; every route but /health checks a secret.
if [[ "$PUBLIC" == "--public" ]]; then
  sprite url update --auth public
fi
sprite info
echo
echo "Next: set FP_PUBLIC_URL in .env.sprite to the URL above, redeploy, then:"
echo "  curl -X POST \"\$FP_PUBLIC_URL/admin/telegram-webhook\" -H \"X-Fine-Print-Secret: \$FP_SHARED_SECRET\""
