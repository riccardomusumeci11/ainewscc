#!/usr/bin/env bash
# panel_hide.sh — close the news pane(s) tagged with @newsdigest in THIS Claude Code
# session's window only, so this CC pane fills its window again without disturbing
# other sessions' panels. Idempotent (no tagged pane → no-op). Also clears the
# WORKING marker. Never crashes; safe outside tmux.
set -uo pipefail

STATE_DIR="$HOME/.cc-learn-banner"
rm -f "$STATE_DIR/panel_pane_id" 2>/dev/null || true

[ -n "${TMUX:-}" ] || exit 0          # only meaningful inside tmux

# Resolve the window we belong to; without it we cannot scope safely, so no-op.
TARGET="${TMUX_PANE:-}"
[ -n "$TARGET" ] || exit 0
WIN="$(tmux display-message -p -t "$TARGET" '#{window_id}' 2>/dev/null)"
[ -n "$WIN" ] || exit 0

tmux list-panes -t "$WIN" -F '#{@newsdigest} #{pane_id}' 2>/dev/null | awk '$1==1{print $2}' \
  | while read -r p; do
      [ -n "$p" ] && tmux kill-pane -t "$p" 2>/dev/null || true
    done
exit 0
