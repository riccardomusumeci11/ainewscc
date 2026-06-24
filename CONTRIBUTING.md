# Contributing to Meanwhile

Thanks for helping out! This project is intentionally tiny and dependency-free, so
contributions are easy to review and run.

## Ground rules

- **Python 3, standard library only.** No `pip install`, no third-party packages.
  If you reach for a dependency, there's almost always a stdlib way.
- **No secrets, ever.** The Gemini key is read only from the `GEMINI_API_KEY`
  environment variable — never hard-code, log, or commit a key.
- **Primary sources only.** Don't add aggregators or curated newsletters to
  `data/feeds.txt`; add only first-party/official feeds you're allowed to read.
- Keep the **legal posture** intact (see [NOTICE](NOTICE)): titles + short
  syndicated extracts + links, with attribution — never article bodies.

## Easy, high-value contributions

- **A new feed.** Add a line to `data/feeds.txt` (it's commented by category).
  Make sure it's a live RSS/Atom feed from a primary source:
  ```sh
  curl -sI <feed-url>            # should be 200
  ./news --self-refresh          # should pick it up without errors
  ```
- **A new concept.** Add a line to `data/concepts.txt` as `CATEGORY │ one dense
  sentence` (English). The **teeth test**: it must teach something *non-obvious to
  an experienced engineer* — a mechanism, a number, or a tradeoff. If a mid-level
  dev would call it obvious, leave it out.

## Working on the code

The whole tool is in a handful of files:

| File              | Role                                                            |
| ----------------- | --------------------------------------------------------------- |
| `news.py`         | fetch · cluster · rank · summarize · the TUI viewer · CLI       |
| `install`         | the installer (hooks, shell wrapper, all modes)                 |
| `panel_show.sh` / `panel_hide.sh` | the tmux split (show on work / hide on wait)    |
| `.github/workflows/digest.yml`    | the daily central build                         |

Quick checks before opening a PR:

```sh
python3 -m py_compile news.py install     # must pass
echo '{}' | python3 news.py --statusline  # must print one line, exit 0
./news --recap                            # non-interactive sanity check
```

Please keep the **no-dependency / no-key-required-for-users** guarantees and the
clean `./install --uninstall` (anything you install must be removable).

## Pull requests

- Keep PRs focused and describe the user-visible change.
- Match the surrounding style (comment density, naming, stdlib idioms).
- Be ready to explain *why* — especially for ranking, clustering, or hook changes.

By contributing you agree to the [Code of Conduct](CODE_OF_CONDUCT.md) and that your
work is licensed under the project's [MIT License](LICENSE).
