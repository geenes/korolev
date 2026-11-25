#!/usr/bin/env bash
# Install Korolev once per machine. It then works for any number of projects.
# - links the skill into ~/.claude/skills/korolev and ~/.codex/skills/korolev
# - creates ~/.config/korolev and KOROLEV_HOME (default ~/korolev)
# - runs the preflight check
# --with-resume also installs the user systemd timer and service.
# Telegram is optional: echo "<BotFather token>" | python3 skill/scripts/setup_telegram.py
set -euo pipefail
case "$#:${1:-}" in
  0:) with_resume=false ;;
  1:--with-resume) with_resume=true ;;
  *) echo "Usage: $0 [--with-resume]" >&2; exit 2 ;;
esac
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
if "$with_resume"; then
  if ! [ "$HOME/.claude/skills/korolev" -ef "$skill" ]; then
    echo "The resume service needs this checkout linked at $HOME/.claude/skills/korolev." >&2
    exit 1
  fi
  unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  mkdir -p "$unit_dir"
  install -m 644 "$skill/systemd/korolev-resume.service" "$skill/systemd/korolev-resume.timer" "$unit_dir/"
  echo 'Enable unattended resume with:'
  echo '  systemctl --user daemon-reload'
  echo '  systemctl --user enable --now korolev-resume.timer'
fi
python3 -I "$skill/scripts/doctor.py" || true
