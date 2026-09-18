# Contributing

Thanks for taking a look. This is a spare-time project, so there is no support promise and reviews can take a
while — but issues and pull requests are welcome.

## Issues

Useful things to include: what you expected, what happened, the version from the sidebar, and the relevant
lines from **System → Logs** or **Activity**. Leave out API keys, passwords and anything else private — and
please use invented publication names in examples.

Security problems go through GitHub's private vulnerability reporting instead; see [SECURITY.md](SECURITY.md).

## Pull requests

- Branch from **`develop`** and open the pull request against it.
- Run the checks first: `ruff check .`, `pytest`, `mypy periodica`, `bandit -c pyproject.toml -r periodica`.
  With Docker, `scripts/test-local.sh all` runs the same things CI does, including the end-to-end tests.
  See [docs/development.md](docs/development.md).
- Add or adjust tests for what you change.
- Keep the safety model in [SECURITY.md](SECURITY.md) intact: one category in scope, nothing written into
  download folders, library folders deleted only when they carry the marker, and no connections beyond the
  configured qBittorrent and Jellyfin.
- Use invented names in code, tests, docs and commit messages (*Evening Post*, *Duck Weekly*, …), never real
  publications.

## Style

Match the code around you: type hints, short focused functions, comments only where the reason isn't obvious
from the code. Line length is 120; ruff and mypy settings live in `pyproject.toml`.
