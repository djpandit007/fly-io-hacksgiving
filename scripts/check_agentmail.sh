#!/usr/bin/env bash
# Check the shared inbox wiring from the Sprite: key scope, inbox access, and webhook secret presence.
# The Sprites AgentMail connector cannot create webhooks, so the webhook is created once in the
# AgentMail console (see README "Email intake"); this script confirms the rest.
# No python here: the Sprite's pyenv shim can hang non-interactive shells.
set -euo pipefail
sprite exec --no-stdin -- bash -c '
  set -euo pipefail
  cd /home/sprite/fine-print
  base=$(grep ^FP_AGENTMAIL_BASE= .env | cut -d= -f2-)
  inbox=$(grep ^FP_AGENTMAIL_INBOX_ID= .env | cut -d= -f2-)
  secret=$(grep ^FP_AGENTMAIL_WEBHOOK_SECRET= .env | cut -d= -f2- | cut -d" " -f1)
  enc=${inbox//@/%40}
  echo -n "key scope:      "; curl -s --max-time 20 "$base/v0/auth/me" | grep -o "\"scope_id\":\"[^\"]*\"" || echo "?"
  echo -n "inbox readable: "; curl -s --max-time 20 -o /dev/null -w "%{http_code}\n" "$base/v0/inboxes/$enc/messages?limit=1"
  if [[ "$secret" == whsec_* ]]; then echo "webhook secret: set"; else echo "webhook secret: MISSING (create the webhook in the AgentMail console)"; fi
'
