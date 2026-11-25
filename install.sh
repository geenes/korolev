#!/usr/bin/env bash
# Install Korolev once per machine. It then works for any number of projects.
# - links the skill into ~/.claude/skills/korolev and ~/.codex/skills/korolev
# - creates ~/.config/korolev and KOROLEV_HOME (default ~/korolev)
# - runs the preflight check
# Telegram is optional: echo "<BotFather token>" | python3 skill/scripts/setup_telegram.py
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
skill="$here/skill"
for dir in "$HOME/.claude/skills" "$HOME/.codex/skills"; do
  mkdir -p "$dir"
  link="$dir/korolev"
  if [ -L "$link" ] || [ ! -e "$link" ]; then
    ln -sfn "$skill" "$link"
    echo "linked $link -> $skill"
  else
    echo "skipped $link: exists and is not a symlink (remove it to install)" >&2
  fi
done
mkdir -p "$HOME/.config/korolev" "${KOROLEV_HOME:-$HOME/korolev}/products" "${KOROLEV_HOME:-$HOME/korolev}/work"
chmod 700 "$HOME/.config/korolev"
python3 -I "$skill/scripts/doctor.py" || true
