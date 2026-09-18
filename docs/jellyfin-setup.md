# Jellyfin setup

Periodica writes files in the layout Jellyfin reads for books. This was checked against Jellyfin's source code, and CI tests it against a real **Jellyfin 12** (`tests/integration/test_jellyfin_it.py`):

- A folder with **exactly one** book file is treated as one book, which is why every issue gets its own folder.
- Jellyfin looks for `<file>.opf`, then `content.opf`, then **`metadata.opf`** next to the PDF. It reads `dc:title`, `dc:date`, `dc:publisher` (shown as studio), `dc:subject` (genre), `dc:language`, `dc:description` and `calibre:series` / `calibre:series_index` / `calibre:title_sort`.
- The local image provider uses **`cover.jpg`** from the issue folder as the primary image and **`folder.jpg`** as the newspaper folder's image.

## Which Jellyfin version?

- **Jellyfin 12 or newer (recommended):** OPF metadata and local covers are built in. The old *Bookshelf* plugin is deprecated in Jellyfin 12, and its online providers are now separate *Google Books* and *ComicVine* plugins. They have nothing to offer a newspaper library; for comics or books, install them if you want extra metadata.
- **Jellyfin 10.11:** install the **Bookshelf** plugin (Dashboard → Plugins → Catalog → *Bookshelf*) and restart Jellyfin. Uninstall it before upgrading to Jellyfin 12, as the Jellyfin 12 release notes advise for repository plugins.

## Steps

1. **Make sure Jellyfin can see the library folder.** Its container needs e.g. `/srv/data/media:/media` (read-only is fine).
2. **Deploy Periodica first.** On startup it creates the destination folder with a hidden `.periodica-library` file. Jellyfin ignores an empty library folder entirely, and a library created on an empty folder stays empty until Jellyfin's next full scan.
3. **Add a library**: Dashboard → Libraries → Add Media Library
   - Content type: **Books**
   - Folder: `/media/books/news`, i.e. the destination folder as Jellyfin sees it
   - **Metadata downloaders / image fetchers**: for a newspaper library leave all online providers off (Jellyfin 12 has none unless you install the Google Books or ComicVine plugins; on 10.11 untick them) — they find nothing for daily issues. For comics or books, turn on whichever ones you want. Local `metadata.opf`, `cover.jpg` and `folder.jpg` are used either way; Jellyfin prefers local files, so online data only fills what is missing.
   - *Real time monitoring* is optional; Periodica triggers a scan of this library through the API.
4. Create an API key in Jellyfin (Dashboard → API Keys) and enter it under **Periodica → Settings → Jellyfin**. Press **Test** and choose this library under *Library to scan*.

## Troubleshooting

- **Covers show "?"**: open the newspaper in Periodica and check the *Cover* column. If it says `failed`, the PDF could not be rendered; use **Re-render cover** after the download is fixed. Then run *Refresh metadata → Replace all images* on the item in Jellyfin.
- **Titles look like file names**: on Jellyfin 10.11 the Bookshelf plugin is missing, or the *Open Packaging Format* metadata reader is disabled for the library.
- **The library stays empty or keeps deleted issues**: Jellyfin's log says *"Library folder … is inaccessible or empty, skipping"*. Check that the folder is mounted in Jellyfin's container and contains `.periodica-library`; Periodica recreates it on startup and every scan.
- **An issue shows up twice**: the destination folder must not be inside another Jellyfin library that also scans it.
- **Sort names look odd in the API**, e.g. `cotenord gazette 0020260915`: that's Jellyfin 12's normal sort-name cleaning; the order in the library is correct.
