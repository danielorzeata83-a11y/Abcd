#!/bin/bash
set -euo pipefail

# Claude Code on the web runs in an ephemeral container, so globally-installed
# agent skills do not survive a reset. Reinstall the mattpocock/skills set on
# each web session start. Local (non-web) sessions are skipped because their
# global installs already persist on disk.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

# Idempotent: re-running just refreshes the skill files/symlinks.
# Verbose installer output goes to a log so it does not flood session context.
log=/tmp/skills-install.log
if npx --yes skills add mattpocock/skills -y -g >"$log" 2>&1; then
  echo "session-start: installed mattpocock/skills (log: $log)"
else
  echo "session-start: WARNING mattpocock/skills install failed; continuing (log: $log)"
fi
