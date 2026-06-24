---
description: Install Meanwhile as a side panel or a Claude Code statusline (or uninstall)
---

You are helping the user install **Meanwhile** from this repository. The repo
ships an `./install` script (Python 3, stdlib only) with three explicit modes.
Do not write to `~/.claude` yourself — let `./install` do it, since it backs up
and merges `settings.json` safely.

Run the mode the user asked for (ask which one if unclear — the two install modes
are mutually exclusive in spirit but can both be active):

- **Panel** (rich side pane, zero risk, does NOT touch `~/.claude`):
  ```sh
  ./install --panel
  ```
  Add `--with-slash-command` ONLY if the user explicitly wants this `/install`
  command copied into `~/.claude/commands/`.

- **Statusline** (one line inside Claude Code; writes `~/.claude/settings.json`
  with a timestamped backup + a careful JSON merge that preserves every other key):
  ```sh
  ./install --statusline
  ```
  After it runs, tell the user the statusline appears on Claude Code's next render
  and that a backup of their previous `settings.json` was made.

- **Uninstall** (undo whichever modes were installed; restores `settings.json` to
  exactly what it was):
  ```sh
  ./install --uninstall
  ```

After running, report what changed (files touched, backup path) in one short
summary. If `./install --statusline` refuses because `settings.json` isn't valid
JSON, relay that and let the user fix it by hand — do not edit it for them.

$ARGUMENTS
