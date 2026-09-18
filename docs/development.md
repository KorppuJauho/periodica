# Development

Python 3.10+, FastAPI and Jinja2, SQLite, no JavaScript build step. The package is `periodica/`, the tests are
in `tests/`.

## Checks

```bash
python -m venv .venv && . .venv/bin/activate
pip install --require-hashes -r requirements-dev.txt
pytest
ruff check . && mypy periodica && bandit -c pyproject.toml -r periodica
```

Dependencies are locked with hashes. To change them, edit `requirements*.in` and re-lock each file:

```bash
uv pip compile requirements.in --python-platform x86_64-manylinux_2_28 --python-version 3.13 --generate-hashes -o requirements.txt
```

## The same checks in Docker (Linux or WSL)

`scripts/test-local.sh` runs what CI runs, including the end-to-end tests:

```bash
scripts/test-local.sh              # lint + unit + integration
scripts/test-local.sh lint         # ruff, mypy, bandit
scripts/test-local.sh unit         # unit tests inside the runtime image (Linux, pdftoppm, non-root)
scripts/test-local.sh integration  # against a real qBittorrent (about a minute)
scripts/test-local.sh jellyfin     # against a real qBittorrent and Jellyfin (a few minutes)
scripts/test-local.sh ui           # the app with a test qBittorrent and sample issues
scripts/test-local.sh down         # stop the test containers and remove their volumes
```

`ui` starts the release image at `http://localhost:8765` (create any admin account; the settings are
pre-filled) and a test qBittorrent at `http://localhost:18080` with sample packs. It is fully isolated from a
real install.

On WSL, use Ubuntu with Docker Engine and your user in the `docker` group (`sudo usermod -aG docker $USER`,
then `wsl --shutdown`). WSL stops when no session is open, taking the `ui` containers with it, so keep a
terminal open while testing.

## Running it without Docker

```bash
python scripts/dev_demo.py --no-auth     # the UI against a fake qBittorrent with sample issues
python -m periodica scan --dry-run       # one scan from the command line
```

## How it fits together

| Module | What it does |
|---|---|
| `scanner.py` | one scan: discover downloads, decide what each file is, link, metadata, covers, retention |
| `sources.py` | where downloads come from: qBittorrent or a watched folder |
| `parser.py`, `patterns.py`, `numbering.py` | reading issue names, including user-taught patterns |
| `formats.py` | which file formats count, checking them, and covers from archives |
| `linker.py` | the library layout: hardlinks, issue folders, the ownership marker |
| `metadata.py`, `covers.py` | `metadata.opf` and cover images |
| `health.py` | checking that linked issues are still whole |
| `retention.py`, `manual_delete.py` | automatic and manual deletion, with their safety checks |
| `jellyfin.py`, `qbittorrent.py`, `netsafe.py` | the two LAN clients and the rules they must obey |
| `db.py`, `repo.py`, `settings.py`, `libraries.py` | storage, migrations and configuration |
| `web/` | FastAPI routes, Jinja2 templates, static files |

Tests mirror that: `tests/test_scanner_retention.py`, `tests/test_formats.py`, `tests/test_health.py` and so
on, with `tests/integration/` for the real-service tests.

## Pull requests

Open them against `develop`. CI runs lint, type checks, security checks, the tests (also inside the runtime
image), the integration tests, a dependency audit and an image scan; all of it has to pass. Keep the safety
model in [SECURITY.md](../SECURITY.md) intact, and use invented publication names in code, tests and examples.
