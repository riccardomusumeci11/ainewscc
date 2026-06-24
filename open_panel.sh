#!/usr/bin/env bash
# open_panel.sh — open a NEW Terminal.app window running the news panel, alongside
# Claude Code, and record its window id + tty so close_panel.sh can close exactly
# that window. Idempotent (no second window if the recorded one is still open).
#
# It binds the window id/tty to the ACTUAL new tab (not "front window", which can be
# a Claude Code window) and refuses to track a window that holds a CC session. It
# does NOT wait for the viewer to finish starting — a heavy ~/.zshrc can delay it,
# and SessionStart hooks must return fast.
#
# First run, macOS asks "Terminal wants to control Terminal" (Automation). Allow it
# (System Settings > Privacy & Security > Automation > Terminal → enable Terminal).
# On a permission failure it prints how to grant it and exits non-zero — never crashes.
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_DIR="$HOME/.cc-learn-banner"
ID_FILE="$STATE_DIR/panel_window_id"
TTY_FILE="$STATE_DIR/panel_window_tty"
ERR="$STATE_DIR/.open_err"
mkdir -p "$STATE_DIR"

perm_msg() {
  echo "open_panel: couldn't control Terminal.app." >&2
  echo "Grant Automation access: System Settings > Privacy & Security > Automation >" >&2
  echo "  Terminal → enable the 'Terminal' checkbox, then run this again." >&2
  [ -s "$ERR" ] && sed 's/^/  osascript: /' "$ERR" >&2
}

# ── Idempotency: if the recorded window is still open, do nothing ──
if [ -f "$ID_FILE" ]; then
  ID="$(cat "$ID_FILE" 2>/dev/null)"
  if [ -n "${ID:-}" ]; then
    OPEN="$(osascript -e "tell application \"Terminal\" to return (exists window id $ID)" 2>/dev/null)"
    if [ "$OPEN" = "true" ]; then
      echo "Panel already open (window id $ID); nothing to do."
      exit 0
    fi
  fi
fi

# ── Open a new window; bind id/tty to the NEW TAB (robust against focus and a slow
#    shell). Don't wait for the viewer — it starts after ~/.zshrc on its own. ──
OUT="$(osascript 2>"$ERR" <<EOF
tell application "Terminal"
    set newTab to do script "cd '$DIR' && exec ./news"
    delay 0.3
    set theWindow to (first window whose tabs contains newTab)
    set hasClaude to false
    repeat with tb in tabs of theWindow
        try
            if ((processes of tb) as string) contains "claude" then set hasClaude to true
        end try
    end repeat
    if hasClaude is false then
        try
            set bounds of theWindow to {900, 40, 1680, 1020}
        end try
    end if
    return (id of theWindow as string) & "|" & (tty of newTab) & "|" & (hasClaude as string)
end tell
EOF
)"
if [ $? -ne 0 ] || [ -z "$OUT" ]; then
  perm_msg
  exit 1
fi

WIN_ID="$(printf '%s' "$OUT" | cut -d'|' -f1)"
WIN_TTY="$(printf '%s' "$OUT" | cut -d'|' -f2)"
HAS_CLAUDE="$(printf '%s' "$OUT" | cut -d'|' -f3)"

# SAFETY: never track a window that also holds a Claude Code session — closing it
# later would close CC. Happens if macOS opened the panel as a TAB in the CC window.
if [ "$HAS_CLAUDE" = "true" ]; then
  echo "open_panel: the panel opened as a TAB inside a Claude Code window, not its" >&2
  echo "  own window — refusing to track it (closing it would close CC)." >&2
  echo "  Fix: System Settings > Desktop & Dock > 'Prefer tabs when opening documents'" >&2
  echo "  → set to 'Never', then run ./open_panel.sh again." >&2
  PT="${WIN_TTY#/dev/}"; [ -n "$PT" ] && pkill -t "$PT" 2>/dev/null || true
  exit 1
fi

printf '%s\n' "$WIN_ID" > "$ID_FILE"
printf '%s\n' "$WIN_TTY" > "$TTY_FILE"
echo "Panel opened in its own window (id $WIN_ID, tty $WIN_TTY). The viewer appears"
echo "after your shell startup finishes."
