# Jellyfin setup

Periodica writes files in the layout Jellyfin reads for books. This was checked against Jellyfin's source code, and CI tests it against a real **Jellyfin 12** (`tests/integration/test_jellyfin_it.py`):

- A folder with **exactly one** book file is treated as one book, which is why every issue gets its own folder.
- Jellyfin looks for `<file>.opf`, then `content.opf`, then **`metadata.opf`** next to the PDF. It reads `dc:title`, `dc:date`, `dc:publisher` (shown as studio), `dc:subject` (genre), `dc:language`, `dc:description` and `calibre:series` / `calibre:series_index` / `calibre:title_sort`.
- The local image provider uses **`cover.jpg`** from the issue folder as the primary image and **`folder.jpg`** as the newspaper folder's image.

## Which Jellyfin version?

**Jellyfin 12 or newer.** That is the only version Periodica has been tried on: CI runs against Jellyfin 12.0
and 12.1, and the maintainer runs 12.x. OPF metadata and local covers are built in there, so nothing needs
installing.

**Older versions are untested.** They may work — a 10.x Jellyfin needs the **Bookshelf** plugin (Dashboard →
Plugins → Catalog → *Bookshelf*) for OPF metadata, and its notes below are written from the plugin's
documentation, not from a real test. There is no guarantee, and problems on 10.x are not something this
project chases. Upgrade to 12 if you can; uninstall Bookshelf before you do, as the Jellyfin 12 release notes
advise for repository plugins.

In Jellyfin 12 the old Bookshelf online providers are separate *Google Books* and *ComicVine* plugins. They
have nothing to offer a newspaper library; for comics or books, install them if you want extra metadata.

## Steps

1. **Make sure Jellyfin can see the library folder.** Its container needs e.g. `/srv/data/media:/media` (read-only is fine).
2. **Deploy Periodica first.** On startup it creates the destination folder with a hidden `.periodica-library` file. Jellyfin ignores an empty library folder entirely, and a library created on an empty folder stays empty until Jellyfin's next full scan.
3. **Add a library**: Dashboard → Libraries → Add Media Library
   - Content type: **Books**
   - Folder: `/media/books/news`, i.e. the destination folder as Jellyfin sees it
   - **Metadata downloaders / image fetchers**: for a newspaper library leave all online providers off (Jellyfin 12 has none unless you install the Google Books or ComicVine plugins; on older versions untick them) — they find nothing for daily issues. For comics or books, turn on whichever ones you want. Local `metadata.opf`, `cover.jpg` and `folder.jpg` are used either way; Jellyfin prefers local files, so online data only fills what is missing.
   - *Real time monitoring* is optional; Periodica triggers a scan of this library through the API.
4. Create an API key in Jellyfin (Dashboard → API Keys) and enter it under **Periodica → Settings → Jellyfin**. Press **Test** and choose this library under *Library to scan*.

## One Jellyfin library for several Periodica libraries

With a *News*, a *Magazines* and a *Comics* library, Jellyfin's navigation fills up with one entry each. To get a
single entry instead, with the libraries as folders inside it, point **one** Books library at their common
parent folder:

```
/media/books/                  ← one Jellyfin library, e.g. "Publications"
  News/        Evening Post/  Chronicle/  …
  Magazines/   Harbour Monthly/  …
  Comics/      Duck Weekly/  …
```

1. Give each Periodica library a destination folder side by side under one parent, e.g.
   `/data/media/books/News`, `/data/media/books/Magazines` and `/data/media/books/Comics`. Jellyfin shows these
   folder names as they are, so name them the way you want them to appear.
2. Keep that parent for these libraries only: anything else under it is scanned too.
3. In Jellyfin, create one **Books** library whose folder is the parent (`/media/books`), and remove the
   separate libraries it replaces — a folder that two Jellyfin libraries scan shows every issue twice.
4. In each Periodica library's settings, choose that one Jellyfin library. The Add-library wizard only
   pre-selects a Jellyfin library whose folder matches the destination exactly, so pick it from the list.

Issues, titles, series and covers come out the same as with separate libraries (checked against a real
Jellyfin 12), and Periodica asks Jellyfin for one refresh even when several of its libraries change in the same
scan. Three things change:

- **A refresh scans the whole library**, since Jellyfin can't refresh part of one. That is more work for Jellyfin
  on each new issue, though nothing you'd normally notice.
- **Library settings are shared.** Metadata downloaders and image fetchers apply to every folder in it, so you
  can't have ComicVine on for comics only.
- **Switching an existing setup recreates the items** in Jellyfin: played state, favourites and collections
  that belonged to the old libraries are lost. The files on disk don't change.

Adding several folders to one Jellyfin library is not the same thing: Jellyfin merges them into one flat list
of publications with no *News* / *Magazines* level, and a publication that exists in two libraries shows up
twice side by side.

## Troubleshooting

- **Covers show "?"**: open the newspaper in Periodica and check the *Cover* column. If it says `failed`, the PDF could not be rendered; use **Re-render cover** after the download is fixed. Then run *Refresh metadata → Replace all images* on the item in Jellyfin.
- **Titles look like file names**: the *Open Packaging Format* metadata reader is disabled for the library — or, on an older Jellyfin, the Bookshelf plugin is missing.
- **The library stays empty or keeps deleted issues**: Jellyfin's log says *"Library folder … is inaccessible or empty, skipping"*. Check that the folder is mounted in Jellyfin's container and contains `.periodica-library`; Periodica recreates it on startup and every scan.
- **An issue shows up twice**: the destination folder is scanned by two Jellyfin libraries — typically a library over the parent folder while the separate ones still exist. Remove one of them.
- **Sort names look odd in the API**, e.g. `cotenord gazette 0020260915`: that's Jellyfin 12's normal sort-name cleaning; the order in the library is correct.
