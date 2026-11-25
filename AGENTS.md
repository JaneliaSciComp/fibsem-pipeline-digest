# Contributing

Setup, usage and the `board.json` format are in [README.md](README.md). This file covers what you need to change the code.

## Rules of thumb

- **Only `digest.py` calls `claude`.** `fetch`, `render`, the CLI and the tests never do, so layout work (`render`) and fetching cost no API calls.
- **Facts come from the board, judgement from Claude.** Column, timeline, owner and links are computed in `render.py`. Claude only writes bullets. Don't ask the prompt for something code can compute.
- **No local state between runs.** Each run fetches everything and writes one `out/<timestamp>/` directory. The only carry-over is the previous run's `board.json`, used as the default window start.
- **The HTML must stay self-contained.** No external assets, so it can be attached to an e-mail.
- Keep modules few. Add a file only when one has clearly outgrown itself.

## Checks

```sh
uv run tests/test_digest.py   # prints "ok"; plain asserts, no pytest
```

The `API overloaded` / `retrying` lines in the output are the retry test simulating a failing `claude`; the run is fine if it ends in `ok`. Tests build snapshots in-code and stub `subprocess.run`, so they need no token and no network. Add a check there for new logic.

To check layout changes, run `uv run -m fibsem_digest render out/<ts>` on an existing run and open the HTML.

## Secrets and data

- `.env` holds `GITHUB_TOKEN` and is gitignored. Never commit it or paste a token into an issue or PR.
- `out/` contains private project discussion and is gitignored. Don't commit run output or paste it into public places.

## Conventions

- Python ≥ 3.12, dependencies managed with `uv` (commit `uv.lock` changes). Prefer stdlib and the existing deps (`httpx`, `pydantic`, `click`) over adding new ones.
- Google-style docstrings; each module starts with a short docstring saying what it does.
- Commit messages: short imperative subject, as in `git log`.
