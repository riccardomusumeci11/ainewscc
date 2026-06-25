#!/usr/bin/env bash
# Meanwhile — one-line installer.
#
#   curl -fsSL https://raw.githubusercontent.com/riccardomusumeci11/meanwhile/main/install.sh | bash
#
# Clones the repo (to ~/meanwhile, or $MEANWHILE_DIR) and runs ./install --quickstart.
# Then you just open a new terminal and run `claude`. Undo with: ./install --uninstall
set -euo pipefail

REPO="https://github.com/riccardomusumeci11/meanwhile.git"
DIR="${MEANWHILE_DIR:-$HOME/meanwhile}"

echo "Meanwhile — one-line installer"
echo

# --- required tools -------------------------------------------------------
missing=""
for c in git python3; do
  command -v "$c" >/dev/null 2>&1 || missing="$missing $c"
done
if [ -n "$missing" ]; then
  echo "ERROR: missing required tool(s):$missing" >&2
  echo "Install them and re-run." >&2
  exit 1
fi

# Claude Code itself is required — Meanwhile splits a pane beside it.
if ! command -v claude >/dev/null 2>&1; then
  echo "note: 'claude' (Claude Code) isn't on your PATH."
  echo "      Meanwhile is an add-on for Claude Code — install it first:"
  echo "      https://claude.com/claude-code"
  echo
fi

# tmux is required for the split side panel. Offer to install it on macOS;
# read from /dev/tty so the prompt works even under  curl … | bash .
if ! command -v tmux >/dev/null 2>&1; then
  echo "'tmux' is not installed — the split side panel needs it."
  if [ "$(uname)" = "Darwin" ] && command -v brew >/dev/null 2>&1 && [ -r /dev/tty ]; then
    printf "Install tmux now with Homebrew? [Y/n] "
    read -r ans < /dev/tty || ans=""
    case "$ans" in
      [Nn]*) echo "Skipping. Install it later with:  brew install tmux"; echo ;;
      *) brew install tmux || { echo "  'brew install tmux' failed — install it manually."; echo; } ;;
    esac
  else
    echo "  Install it first, then re-run this installer:"
    echo "    macOS:          brew install tmux"
    echo "    Debian/Ubuntu:  sudo apt install tmux"
    echo
  fi
fi

# --- clone or update ------------------------------------------------------
if [ -d "$DIR/.git" ]; then
  echo "Updating existing clone at $DIR …"
  git -C "$DIR" pull --ff-only
else
  echo "Cloning into $DIR …"
  git clone --depth 1 "$REPO" "$DIR"
fi
echo

# --- set up ---------------------------------------------------------------
cd "$DIR"
./install --quickstart

echo
echo "──────────────────────────────────────────────────────────────"
echo "Done.  Open a NEW terminal and run:   claude"
echo "(Meanwhile splits in beside Claude Code while it works.)"
echo "Undo anytime:  cd \"$DIR\" && ./install --uninstall"
