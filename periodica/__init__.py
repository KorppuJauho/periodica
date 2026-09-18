"""periodica: newspaper PDFs from qBittorrent into a Jellyfin Books library."""

__version__ = "1.0.0"


def app_version() -> str:
    """The release version for images built from a version tag, otherwise "<next version>-dev"."""
    import os
    import re

    released = os.environ.get("PERIODICA_VERSION", "")
    return released if re.fullmatch(r"\d+\.\d+\.\d+", released) else f"{__version__}-dev"


def build_label() -> str:
    """Version plus the short Git commit baked into the image at build time (e.g. "1.0.0 · a0d8210")."""
    import os
    import re

    sha = os.environ.get("PERIODICA_COMMIT", "")
    sha = sha[:7] if re.fullmatch(r"[0-9a-f]{7,40}", sha) else ""
    return f"{app_version()} · {sha}" if sha else app_version()
