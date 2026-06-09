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

# mindrally/skills: 240+ Cursor-rules guideline skills (pure SKILL.md, no manifest),
# so the `skills` tool above cannot ingest it. Clone and copy each skill into the
# global skills dir. No-clobber: never overwrite a skill from the curated repos above.
mr_tmp="$(mktemp -d)"
if git clone --depth 1 https://github.com/mindrally/skills.git "$mr_tmp/repo" >>"$log" 2>&1; then
  dest="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
  mkdir -p "$dest"
  copied=0
  for d in "$mr_tmp"/repo/*/; do
    name="$(basename "$d")"
    [ -f "$d/SKILL.md" ] || continue
    if [ -e "$dest/$name" ]; then continue; fi
    if cp -r "$d" "$dest/$name"; then copied=$((copied + 1)); fi
  done
  echo "session-start: installed mindrally/skills ($copied skills, no-clobber; log: $log)"
else
  echo "session-start: WARNING mindrally/skills clone failed; continuing (log: $log)"
fi
rm -rf "$mr_tmp"
