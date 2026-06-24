#!/usr/bin/env bash
# panel_show.sh — split a news pane to the RIGHT of the Claude Code pane (same tmux
# window). The news pane is tagged with the tmux pane option @newsdigest so we can
# recognize and dedupe it reliably (Claude Code fires hooks in parallel, so several
# panel_show.sh can race). The check/open/dedup are scoped to the CC pane's OWN
# window so multiple Claude Code sessions each get their own panel independently.
# If a tagged pane already exists in this window, do nothing; if a race created more
# than one, keep the first and kill the rest. No-op outside tmux.
set -uo pipefail

[ -n "${TMUX:-}" ] || exit 0          # only meaningful inside tmux

DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_DIR="$HOME/.cc-learn-banner"
mkdir -p "$STATE_DIR"

# In tmux mode the pane's presence IS the signal, so keep the viewer showing pills.
printf 'WORKING\n' > "$STATE_DIR/cc_state" 2>/dev/null || true

# We must know which CC pane (hence which window) we belong to; without it we cannot
# scope safely across sessions, so do nothing rather than touch another session.
TARGET="${TMUX_PANE:-}"
[ -n "$TARGET" ] || exit 0
WIN="$(tmux display-message -p -t "$TARGET" '#{window_id}' 2>/dev/null)"
[ -n "$WIN" ] || exit 0

# Already a news pane in THIS window? then nothing to do.
if tmux list-panes -t "$WIN" -F '#{@newsdigest}' 2>/dev/null | grep -qx 1; then
  exit 0
fi

# Split the CC pane (~38% wide, to the right), don't steal focus; tag it as ours and
# record the owning CC pane for diagnostics.
NEW="$(tmux split-window -h -d -l 38% -t "$TARGET" -P -F '#{pane_id}' "$DIR/news" 2>>"$STATE_DIR/.tmux_err")"
if [ -n "${NEW:-}" ]; then
  tmux set-option -p -t "$NEW" @newsdigest 1 2>/dev/null || true
  tmux set-option -p -t "$NEW" @newsdigest_owner "$TARGET" 2>/dev/null || true
fi

# De-dup within THIS window: if parallel hooks created more than one, keep the first,
# kill the rest. Other windows/sessions are left untouched.
tmux list-panes -t "$WIN" -F '#{@newsdigest} #{pane_id}' 2>/dev/null | awk '$1==1{print $2}' \
  | tail -n +2 | while read -r p; do
      [ -n "$p" ] && tmux kill-pane -t "$p" 2>/dev/null || true
    done
exit 0
