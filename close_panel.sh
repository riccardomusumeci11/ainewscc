#!/usr/bin/env bash
# close_panel.sh — close ONLY the news panel window opened by open_panel.sh
# (identified by the saved window id). It first terminates the panel's own
# processes (by the saved tty) so Terminal won't prompt about running processes,
# then closes that one window. Other windows are never touched. Never crashes.
set -uo pipefail

STATE_DIR="$HOME/.cc-learn-banner"
ID_FILE="$STATE_DIR/panel_window_id"
TTY_FILE="$STATE_DIR/panel_window_tty"
ERR="$STATE_DIR/.close_err"

if [ ! -f "$ID_FILE" ]; then
  echo "No panel window recorded; nothing to close."
  exit 0
fi
ID="$(cat "$ID_FILE" 2>/dev/null)"

# SAFETY: never act on a window that holds a Claude Code session (closing it, or
# pkill'ing its tty, would kill CC). Only proceed if it's genuinely our panel.
if [ -n "${ID:-}" ]; then
  STATUS="$(osascript 2>"$ERR" <<EOF
tell application "Terminal"
    if not (exists window id $ID) then return "gone"
    set cc to false
    repeat with tb in tabs of window id $ID
        try
            if ((processes of tb) as string) contains "claude" then set cc to true
        end try
    end repeat
    if cc then
        return "claude"
    end if
    return "panel"
end tell
EOF
)"
  if [ "$STATUS" = "claude" ]; then
    echo "close_panel: window $ID contains a Claude Code session — refusing to close it." >&2
    rm -f "$ID_FILE" "$TTY_FILE"
    exit 1
  fi
fi

# Terminate the panel's own processes (its tty) so closing won't prompt.
if [ -f "$TTY_FILE" ]; then
  T="$(cat "$TTY_FILE" 2>/dev/null)"; T="${T#/dev/}"
  [ -n "${T:-}" ] && pkill -t "$T" 2>/dev/null || true
fi

# Close only that window, if it still exists.
if [ -n "${ID:-}" ]; then
  osascript 2>"$ERR" <<EOF
tell application "Terminal"
    if (exists window id $ID) then close window id $ID saving no
end tell
EOF
fi

rm -f "$ID_FILE" "$TTY_FILE"
echo "Panel closed (window id ${ID:-?})."
