# Changelog

All notable changes to Periodica. Versions follow [semantic versioning](https://semver.org/):
**major** = breaking changes (settings, layout or deployment need attention), **minor** = new features,
**patch** = fixes only.

## [Unreleased]

_Nothing yet._

## [1.0.0]

First public release.

### Linking
- Hardlinks issues from a qBittorrent category into a Jellyfin **Books** library: one folder per issue, with
  `metadata.opf` and a cover, and the newest cover as the publication folder's artwork.
- **PDF, CBZ, CBR and EPUB**. When a download holds the same issue in several formats, the library's format
  priority decides which one is linked (`cbz, pdf, epub, cbr` by default); the rest belong to that issue.
- Covers come from a PDF's first page (`pdftoppm`), a CBZ's first page image or an EPUB's own cover image.
  Nothing is fetched from the internet.
- Daily, monthly and numbered issues, with per-publication rules for telling a month from an issue number, and
  automatic relabelling when a publication turns out to be numbered after all.
- **Your own naming patterns** per library, made from the Unmatched page, for names the built-in rules don't
  read. Patterns are compiled from fixed placeholders and never used as regular expressions.
- Unmatched files are listed rather than guessed at, and a download containing one is never deleted.

### Libraries
- **Several libraries**, each with its own category, source and destination folders, Jellyfin library, title
  formats, language, cover width, format priority, allowed extra file types and days to keep.
- An **Add-library wizard** that reads qBittorrent and Jellyfin and suggests the rest; it never creates
  anything in either.
- Libraries can be disabled or removed, and are kept strictly apart from each other.

### Keeping the library right
- **Library health**: every scan checks that linked issues are whole (folder and file present, the file still a
  hardlink to the download, metadata and cover present). Problems are reported, never repaired silently, and
  are fixed or removed on your word.
- **Automatic delete** (off by default): a dry-run preview, an explicit arming step, a grace period with
  *Keep*, and a per-run safety limit. A pack is deleted only once every publication in it has expired.

### Running it
- Works **without Jellyfin**, **without qBittorrent** (watching a folder instead), or without both.
- **Offline mode** (`OFFLINE_MODE=true`) locks every outgoing connection off while keeping the web UI.
- **Scan API** for a "run on torrent finished" call, with its own key, and a switch to turn it off.
- Web UI with dashboard, publications, issues, deletions, unmatched files, activity and live logs.

### Safety
- Only the chosen category is ever in scope; downloads are never written to; library folders are deleted only
  when they carry Periodica's marker.
- LAN-only clients, hardened container, hash-locked dependencies, and CI running lint, type, security and
  end-to-end tests against real qBittorrent and Jellyfin versions. See [SECURITY.md](SECURITY.md).
