# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Instead, use GitHub's
**“Report a vulnerability”** (Security → Advisories) on this repository, or contact
the maintainer privately. You'll get an acknowledgement as soon as possible, and
we'll work with you on a fix and coordinated disclosure.

## What this project does and doesn't touch

- **No secrets are stored or committed.** The only secrets are the optional
  summarizer API keys (`GEMINI_API_KEY`, and the free fallbacks `GROQ_API_KEY` /
  `CEREBRAS_API_KEY`), read exclusively from the environment (and, for the central
  feed, from GitHub Actions Secrets). They are never written to disk or logged.
- **The installer is conservative with `~/.claude/settings.json`:** it backs the
  file up first, merges only its own keys, refuses to write if the JSON is invalid,
  and `./install --uninstall` removes exactly what it added.
- **Hooks are minimal and non-blocking.** They run tiny shell snippets (e.g. a tmux
  split/kill), always exit 0, and never read or write your conversation data.
- **The digest only reads public feeds and a published JSON**, sends a descriptive
  User-Agent, and never executes remote content.

If you find anything that breaks these guarantees, that's a security report —
thank you for letting us know.
