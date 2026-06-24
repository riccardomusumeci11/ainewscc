# Meanwhile — the anti-brainrot side pane for Claude Code

[![CI](https://github.com/riccardomusumeci11/meanwhile/actions/workflows/ci.yml/badge.svg)](https://github.com/riccardomusumeci11/meanwhile/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.x%20stdlib--only-3776AB.svg)](#requirements)
![No dependencies](https://img.shields.io/badge/dependencies-none-success.svg)
![Platform](https://img.shields.io/badge/platform-macOS%20%C2%B7%20Linux-lightgrey.svg)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

When **Claude Code is busy working**, a news + learning pane **splits in beside it**
— and disappears the moment it needs you. Instead of rotting in another tab, you
glance right and **learn or stay current** in 2–3 seconds: today's AI launches, and
dense, non-obvious engineering concepts on a spaced-repetition schedule.

**No API key, no account, no cost.** The digest is built and summarized once a day
by the project and pulled automatically by your client — you install once and never
touch it again.

```
┌───────────────────────────────────┬─────────────────────────────────┐
│ Claude Code                       │ ▸ NEWS                 3/150    │
│                                   │ ─────────────────────────────── │
│ > refactor the auth module        │ OpenAI launches Daybreak to     │
│ ● Working… editing auth.py        │ secure every organization       │
│   running tests                   │ ↻ summarized · OpenAI · ↗ link  │
│                                   │                                 │
│                                   │ ▸ CONCEPT PYTHON │ the GIL is   │
│                                   │ released around blocking I/O…   │
└───────────────────────────────────┴─────────────────────────────────┘
   while CC works → news on the right    ·    CC waits for you → full-width
```

---

## Quickstart

Requires [`tmux`](https://github.com/tmux/tmux) (`brew install tmux`) and the
[Claude Code](https://claude.com/claude-code) CLI.

**One line** — clones to `~/meanwhile` and sets everything up:

```sh
curl -fsSL https://raw.githubusercontent.com/riccardomusumeci11/meanwhile/main/install.sh | bash
```

<details>
<summary>Prefer to see each step? Do it by hand instead.</summary>

```sh
git clone https://github.com/riccardomusumeci11/meanwhile.git
cd meanwhile
./install --quickstart      # tmux split hooks + a `claude` wrapper, in one shot
```
</details>

Then open a **new terminal** and just run:

```sh
claude
```

Claude Code starts inside tmux; the digest pane appears on the right while it works
and closes (CC full-width) while it waits for you. **That's it.**

> Undo everything cleanly at any time: `./install --uninstall`.

---

## How it works

**You never need a key, and the pills update on their own.** The expensive part —
fetching feeds, clustering, ranking, and summarizing — runs **once a day in CI**
with the maintainer's Gemini key; the result is published as a small JSON that your
client pulls:

```
  maintainer's CI (daily) ──build + summarize──▶ dist/news_cache.json  (the `dist` branch)
                                                          │  client pulls when >24h old
   your machine:  ./news ──◀── pulls central feed ────────┘
                    └─ interleaves YOUR concepts (spaced repetition, local) ─▶ the pane
```

- **Offline-safe:** no network → it keeps the last copy and never crashes.
- **Concepts are local:** the central feed is news only; the evergreen concepts and
  your spaced-repetition schedule live on your machine.
- **Self-host instead:** `./news --self-refresh` builds the digest from the raw
  feeds with **your own** `GEMINI_API_KEY` (read only from the environment).

And the pane is wired to Claude Code's lifecycle: tmux hooks **show** the news pane
when CC starts working (a prompt or any tool use) and **hide** it the moment CC
asks for approval or waits for you — so the news is there while you'd otherwise be
idle, and gone when it's your turn.

---

## What you see

- **NEWS — AI news.** Headlines from **primary sources only** (official lab blogs,
  reputable AI press, individual technical voices, arXiv), clustered across outlets
  (one story → one pill listing every source + link), ranked by objective signals,
  and rewritten into neutral 3-to-5-sentence summaries — **clearly labeled as
  AI-generated** (and may contain errors), always attributed and linked.
- **CONCEPT — evergreen concepts.** Dense, non-obvious lessons (KV cache, the GIL,
  MVCC, false sharing, RoPE…) resurfaced on a **spaced-repetition** schedule so they
  actually stick. News you consume and forget; concepts you repeat and keep.

> A **CHANGELOG/RELEASE** pill type for your stack's GitHub releases also exists but
> is **off by default** — uncomment the `changelog:` lines in `data/feeds.txt` to
> enable it.

**Design bar — maximum signal per second.** Every pill must be absorbable with the
corner of your eye in 2–3 seconds, self-contained (no click-through needed), and
abandonable at no cost. It's the opposite of brainrot: it fills the gap with the
densest useful thing you can take in without leaving your task.

---

## Configuration

Everything you curate is plain, hand-editable text — *if you can't read it with
`cat`, it doesn't belong here.*

| File                  | What                                                  | Tracked |
| --------------------- | ----------------------------------------------------- | ------- |
| `data/feeds.txt`      | sources (RSS/Atom, arXiv, optional HN/changelog)      | ✅      |
| `data/concepts.txt`   | evergreen lessons, `CATEGORY │ one dense sentence`    | ✅      |
| `data/news_cache.json`| the local view cache (generated)                      | —       |
| `data/saved.txt`      | pills you pressed `s` to save (personal)              | —       |
| `data/srs_state.json` | your spaced-repetition schedule (personal)            | —       |

**Add a source** — one per line in `data/feeds.txt`:

```
https://example.com/feed.xml                                 # any RSS / Atom feed
arxiv:cs.CL                                                  # an arXiv category
changelog:https://github.com/fastapi/fastapi/releases.atom   # a GitHub release feed
hn:"language model"                                          # a Hacker News query
```

Add only **primary** sources you're authorized to read; dead/unparseable feeds are
skipped automatically.

**Add a concept** — one per line in `data/concepts.txt`, `CATEGORY │ a single dense
sentence`, in English. The teeth test: each must teach something **non-obvious to an
experienced engineer** — a mechanism, a number, or a tradeoff.

**Point at a different feed** — set `NEWS_FEED_URL` to your own published
`news_cache.json` (see [Self-hosting](#self-hosting)).

---

## Usage & keys

```sh
./news                 # the interactive viewer (also what the pane runs)
./news --recap         # print the week's top items and exit (non-interactive)
./news --self-refresh  # build the digest locally with your own GEMINI_API_KEY
./news --interval 60   # base rotation seconds (dwell scales with pill length)
```

| Key                   | Action                                          |
| --------------------- | ----------------------------------------------- |
| `n` / `→` / `space`   | next pill                                       |
| `p` / `←`             | previous pill                                   |
| `↑` / `↓` (`k`/`j`)   | scroll a long, multi-source pill                |
| `o`                   | open the top source link in your browser        |
| `s`                   | **save** the current pill to `data/saved.txt`   |
| `q`                   | quit (terminal restored cleanly)                |

In the tmux split, switch focus to the pane with `Ctrl-b →` (and back with
`Ctrl-b ←`). The pane **remembers its pill** across restarts, the **order is the
ranking** (recency × multi-source coverage × concrete-launch × source authority),
and **dwell time scales with length** so dense pills don't scroll away.

---

## Legal posture

It is a **headline aggregator of primary sources**. Per item it shows only the
**title, source, date, a short syndicated extract (or an LLM rewrite of that
extract), and the link** — **never the article body**, never scraped pages. Every
item is attributed and links back to the publisher; summaries are derived solely
from the publisher's own RSS extract. Full details in [NOTICE](NOTICE).

**Removal / takedown.** If you're a publisher or rights holder and want your source
excluded, **[open an issue](https://github.com/riccardomusumeci11/meanwhile/issues/new?labels=takedown&title=Takedown%20request)**
(label `takedown`). The feed is removed from `data/feeds.txt` and the central feed
rebuilt promptly — no questions asked.

**Disclaimer.** Meanwhile is an **independent, non-commercial, open-source** project.
It is **not affiliated with, endorsed by, or sponsored by** Anthropic, OpenAI,
Google, or any source it links to. *Claude* and *Claude Code* are trademarks of
Anthropic; all other product names, logos, and brands are the property of their
respective owners and are used for identification (nominative) purposes only.

---

## Requirements

- **Claude Code** CLI, **tmux**, and **Python 3** — that's it.
- **Zero Python dependencies:** standard library only (no `pip install`). The
  optional self-hosting summaries use a **free** Gemini key you provide.
- macOS and Linux.

---

## Self-hosting

Run your own daily feed (your own key, your own fork):

1. Fork the repo and add a repository **Secret** `GEMINI_API_KEY`
   ([free key](https://aistudio.google.com/apikey)).
2. The included Action `.github/workflows/digest.yml` builds `news_cache.json` daily
   (and on demand) and force-pushes it to the **`dist`** branch.
3. Point clients at it with `NEWS_FEED_URL` (or edit `REMOTE_URL` in `news.py`).

**Summarizer fallbacks (optional, all free).** Summaries run through a provider
chain — **Gemini → Groq → Cerebras** — so a rate limit or outage on one doesn't
leave cards un-summarized. Add any of these as Secrets (or env vars for
`--self-refresh`) and they're used in order; set none and pills keep their raw RSS
extract:

| Secret              | Free key                                            |
| ------------------- | --------------------------------------------------- |
| `GEMINI_API_KEY`    | <https://aistudio.google.com/apikey>                |
| `GROQ_API_KEY`      | <https://console.groq.com/keys>                     |
| `CEREBRAS_API_KEY`  | <https://cloud.cerebras.ai>                          |

---

## Other layouts

<details>
<summary>Separate window, statusline, and à-la-carte install modes</summary>

`./install --quickstart` is the recommended path. The individual modes it composes
(and a couple of alternatives) are also available — each merges into
`~/.claude/settings.json` with a timestamped backup, preserves every other key, and
refuses to write on invalid JSON; `--uninstall` reverses any of them:

```sh
./install --tmux         # the split hooks only (no shell wrapper)
./install --cc-aware     # state hooks: the panel hides while CC waits (any layout)
./install --statusline   # one cache-only line inside Claude Code's status line
./install --lifecycle    # macOS: auto-open/close a separate Terminal window
./install --panel        # make scripts executable + a `ccnews` tmux launcher (no ~/.claude)
./install --uninstall    # undo any of the above (hooks, scripts, shell wrapper, state)
```

On **macOS Terminal.app** (which can't split panes), `--lifecycle` opens a separate,
tiled Terminal window via AppleScript and closes it on session end; the first run
asks for the *Automation* permission (**System Settings → Privacy & Security →
Automation → Terminal**). The **statusline** view is cache-only, never touches the
network, and is time-gated so it never flickers when re-run.

</details>

---

## Contributing

PRs welcome — especially new **primary** feeds and high-bar **concepts**. See
[CONTRIBUTING.md](CONTRIBUTING.md). By participating you agree to the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Uninstall

```sh
./install --uninstall    # removes hooks, the shell wrapper, and runtime state
rm -rf meanwhile          # then delete the folder; nothing is installed elsewhere
```

## License

[MIT](LICENSE). Aggregated content remains the property of its publishers — see
[NOTICE](NOTICE).
