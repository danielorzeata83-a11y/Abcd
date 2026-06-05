#!/bin/bash
set -euo pipefail

# Claude Code on the web runs in an ephemeral container, so globally-installed
# agent skills do not survive a reset. Reinstall the skill sets on each web
# session start. Local (non-web) sessions are skipped because their global
# installs already persist on disk.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

# Skill repos to (re)install. Idempotent: re-running just refreshes files/symlinks.
repos=(
  "mattpocock/skills"
  "forrestchang/andrej-karpathy-skills"
  "anthropics/skills"
)

# Verbose installer output goes to a log so it does not flood session context.
log=/tmp/skills-install.log
: >"$log"
for repo in "${repos[@]}"; do
  if npx --yes skills add "$repo" -y -g >>"$log" 2>&1; then
    echo "session-start: installed $repo (log: $log)"
  else
    echo "session-start: WARNING $repo install failed; continuing (log: $log)"
  fi
done
