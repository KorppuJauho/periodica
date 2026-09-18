# Which files are linked, and under what name

## The files

Issue files — **PDF, CBZ, CBR and EPUB** — whose name says which issue they are. Everything else is listed on
the **Unmatched** page, and a download containing an unmatched file is never deleted, automatically or by hand.
For an unmatched issue file you can:

- **Fix** — teach Periodica the name (see [Your own naming patterns](#your-own-naming-patterns)); it is then
  linked like any other issue.
- **Ignore** — it stops protecting its download and is deleted along with it (*Undo* is on the same page).

Files that come along with the issues (`.nfo`, `.txt`, `.jpg`, …) are fine once their type is listed under
**Other files allowed in a download** on the library's page (`nfo` by default): they are skipped, never linked,
and deleted with the download. Any other file type keeps the download from being deleted.

**One file per issue.** When a download has the same issue in several formats (`DW.2026.No.38.pdf` and
`DW.2026.No.38.cbr`), only the first in the library's **Format priority** is linked — `cbz, pdf, epub, cbr` by
default, with cbr last because no cover can be read from a RAR archive. The other files belong to that issue:
they are not unmatched and are deleted with the download. If the preferred file turns out to be damaged, the
next format is linked and Activity says so. An issue already in the library keeps its file; a later download of
the same issue is counted as *already in the library from another download*.

**Covers.** PDF: page 1, rendered by `pdftoppm`. CBZ: the first page image. EPUB: the cover the book names.
Images from archives are copied as they are (`cover.jpg`, `cover.png`, `cover.webp`) and never resized or
decoded. CBR (RAR) archives are not opened, so Periodica makes no cover for them; Jellyfin may show its own.

## Names the built-in rules read

| Kind | Example file | In the library |
|---|---|---|
| Daily | `Evening.Post.2026.09.15.pdf`, `Evening Post 15.09.2026.pdf` | `Evening Post/Evening Post 2026-09-15/` |
| Monthly | `Business.Monthly.2026.09.pdf`, `Business Monthly 09-2026.pdf` | `Business Monthly/Business Monthly 2026-09/` |
| Numbered | `Duck.Weekly.2026.38.cbz`, `Duck Weekly 38-2026.cbz` | `Duck Weekly/Duck Weekly 2026 #38/` |

Years from 1900 to 2100 are accepted. A daily name may carry a tag after the date (`…2026.09.15.FINAL.pdf`); a
monthly or numbered name must end with the number, so an impossible date such as `…2026.02.30` is never
mistaken for February.

## Your own naming patterns

Write the name the way it looks, with placeholders for the parts that change: `DW.{year}.No.{number}` reads
`DW.2026.No.38.pdf`.

| Placeholder | Meaning |
|---|---|
| `{year}` | required, 1900–2100 |
| `{number}` | an issue number |
| `{month}` | a monthly issue |
| `{month}` + `{day}` | a daily issue |
| `{paper}` | the publication name, taken from the file |
| `{any}` | text to skip |

Dots, spaces, dashes and underscores match each other and letters match in any case, so the pattern above also
reads `dw 2026 no 38.pdf`. Each pattern names its **publication**: with *Duck Weekly*, that file is linked as
*Duck Weekly 2026 #38* in the *Duck Weekly* folder, so Jellyfin shows Duck Weekly, never "DW".

Patterns are made from the **Unmatched** page with the *Fix* button: the form suggests a pattern from the file
name, previews the result and lists other unmatched files it would read. A library's patterns are tried before
the built-in rules, only in that library, and are listed on the library's page. Your text is never used as a
regular expression. For names the built-in rules already read, the **display name** on a publication's page
renames them the same way.

## Month or issue number?

`Business.Monthly.2026.09` could be September or issue 9, so Periodica decides per publication, and the first
rule that applies wins:

1. **Your choice** on the publication's page (*Monthly* or *Numbered*) — always wins, any time.
2. A number **above 12** is an issue number.
3. A publication once recognised as **numbered stays numbered**.
4. The number is compared with the month of the **daily papers in the same download** (or the download date if
   there are none). The same month, or one either side, means monthly; two or more months away means numbered.
   A back issue from another year proves nothing.
5. In **January**, 12, 1 and 2 are where numbered magazines start their year too, so an undecided publication
   waits on the Unmatched page with *Monthly* / *Numbered* buttons. A second, different number in the same
   January settles it as numbered.

When a publication turns out to be numbered after all, the issues already in the library are relabelled
automatically (folders renamed, metadata rewritten) and Activity says so. Each kind has its own title format on
the library's page.

## Library health

Every scan checks that the issues already in the library are whole:

- the issue folder and its file exist;
- the file is still a hardlink to the download, not a copy or another file;
- `metadata.opf` exists, and the cover too once one has been made.

A damaged issue is **not repaired automatically**, not even a folder you deleted by hand. It is listed on
**System → Library health** (and counted on the dashboard) until you choose:

- **Fix** repairs it on the next scan, which starts right away: linked again, a copy replaced with a link to
  the download (only in folders carrying Periodica's marker, and you confirm first), missing metadata or cover
  written again. Jellyfin is asked to refresh afterwards. **Fix all** does the lot.
- **Remove** takes the issue out of the library for good; it is not linked again.

If the download is gone as well, only Remove is offered. If you put the file back yourself, the problem clears
on the next scan.
