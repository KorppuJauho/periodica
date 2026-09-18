# Setup

About 20 minutes from nothing to a working install. Example paths are only examples; replace them and anything
in `<angle brackets>` with your own.

## What you need

- **Docker** on the machine that stores the downloads.
- **qBittorrent** with its Web UI on, and torrents and media on **the same volume**, e.g. `/srv/data/torrents`
  and `/srv/data/media`.
- **Jellyfin** (optional — see [Without qBittorrent or Jellyfin](#without-qbittorrent-or-jellyfin)).

## 1. Prepare qBittorrent

1. Create a **category** named `news` (right-click the categories list → *Add category*).
2. Find the user qBittorrent runs as, which Periodica must use too:
   ```bash
   docker exec <qbittorrent-container> id
   ```
   `uid=1000(abc) gid=1000(abc)` means `PUID=1000` and `PGID=1000`.

## 2. Deploy Periodica

1. Create a config folder owned by that user:
   ```bash
   sudo mkdir -p /srv/docker/periodica/config && sudo chown <PUID>:<PGID> /srv/docker/periodica/config
   ```
2. Deploy [`docker-compose.yml`](../docker-compose.yml), as a stack in your Docker manager or with
   `docker compose up -d`, with these variables (see [`.env.example`](../.env.example)):

   | Variable | Example | What it is |
   |---|---|---|
   | `PUID` / `PGID` | `1000` / `1000` | from step 1 |
   | `DATA_PATH` | `/srv/data` | the folder holding **both** `torrents/` and `media/` |
   | `CONFIG_PATH` | `/srv/docker/periodica/config` | the folder you just created |
   | `TZ` | `Etc/UTC` | your time zone |

3. Open `http://<server-ip>:8765` and **create the admin account**. There is no default login.

**Why one `DATA_PATH:/data` mount?** Hardlinks only work inside one filesystem *and* one bind mount. Mounting
`torrents` and `media` separately makes every link fail with "cross-device link". The **System** page checks it.

## 3. Settings

1. **Settings → Download client**
   - **URL**: `http://<server-ip>:8080`, plus the Web UI username and password.
   - **Remote path mapping**: leave both empty if qBittorrent mounts the same folder as `/data`. Otherwise
     *remote* = the save path qBittorrent shows, *local* = the same folder under `/data`.
   - **Test**, then **Save** and confirm the dry run.
2. **Settings → Libraries → News**: set **Category** to `news`. The folder defaults usually fit.
3. **System**: every check green, especially *same filesystem (hardlinks possible)*.

## 4. Jellyfin

1. **Jellyfin 12 or newer:** nothing to install. **Jellyfin 10.11:** install the **Bookshelf** plugin and restart.
2. Add a library: content type **Books**, folder = the media folder as Jellyfin sees it (e.g.
   `/media/books/news`). Details, including which metadata fetchers to leave on, are in
   [jellyfin-setup.md](jellyfin-setup.md).
3. Jellyfin → Dashboard → **API Keys** → create a key named `periodica`.
4. Periodica → **Settings → Jellyfin**: the URL and that key → **Test** → pick the library → **Save**.

Add a torrent to the `news` category and press **Scan now**. The issues should appear in Jellyfin with covers.

---

## Automations

### A. Download new issues automatically (qBittorrent RSS)

qBittorrent's own RSS downloader does this. Periodica never fetches feeds itself, so it needs no internet
access and never sees your feed key.

1. qBittorrent → **RSS** → **New subscription** → the feed URL.
2. **Mark the old items read** (right-click the feed), or the rule downloads everything already listed.
3. **RSS Downloader** → a rule with ✔ *Use Regular Expressions*, a **Must Contain** pattern such as
   `^daily newspapers \d{2} \d{2} \d{4}$`, **Assign Category** `news`, *Ignore Subsequent Matches* `0`,
   *Smart Episode Filter* off. Check *Matching RSS Articles* before saving.
4. qBittorrent → Options → **RSS**: enable fetching and auto downloading.

Uploaders rename their packs, so if downloads stop arriving, compare the rule with the current feed titles
first; an alternation like `^(daily|morning) newspapers …` survives a rename. **System** shows a green check
when the rule points at a library's category, and the dashboard warns per library if nothing has arrived for
36 hours (configurable under Settings → Download client).

### B. Scan as soon as a download finishes

1. Periodica → **System** → *Show API key*.
2. qBittorrent → Options → **Downloads** → *Run external program* → ✔ **Run on torrent finished**, on one line:
   ```bash
   curl -fsS -m 10 -X POST -H "X-Api-Key: <API-KEY>" --data-urlencode "category=%L" http://<server-ip>:8765/api/v1/scan
   ```
   `%L` is the category; anything that isn't one of your libraries' categories is ignored. One command serves
   every library.
3. Test it: run the same command inside the qBittorrent container. `{"status":"scan queued"}` and an Activity
   line mean it works. If it doesn't, see [troubleshooting.md](troubleshooting.md).

**Don't want it?** System → Status → *Turn API off*: `/api/v1/scan` then answers `404` to everyone and the key
is kept for later.

**Scan interval:** with this working you can set **Settings → Libraries → Scan interval** to `0`, so scans
happen on a finished download, on *Scan now* and at container start. A larger value (e.g. `120`) is a safety
net if a trigger is ever missed.

### C. Delete old issues automatically (optional)

1. **Settings → Automatic delete**: enable, *Delete after* e.g. `7` days, *Grace period* `24` h.
2. **Deletions**: read the dry-run preview, tick the confirmation and **Arm**.
3. Expired items wait in **Pending** for the grace period, where *Keep* protects them. Then qBittorrent deletes
   the torrent **with its files**, and the issues leave the library.

Each library has its own *Delete after (days)*; the switch, arming, grace period and safety limit are shared. A
pack is deleted only once **every** publication in it has expired, and per-publication overrides live on each
publication's page. You can also delete one pack from **Deletions → Torrents → Delete…**.

### Checklist

- [ ] **System**: all checks green.
- [ ] A new pack arrives → Activity: *API scan (torrent finished in 'news'): N linked…*
- [ ] Issues with covers appear in Jellyfin, and Activity says the library scan was requested.
- [ ] *(If automatic delete is armed)* after N days the pack appears under **Pending**, then in **History**.

---

## Libraries

A library is one qBittorrent category linked into one Jellyfin library, for example *News* and *Magazines*.

**Add one:** Settings → Libraries → **Add library**. The wizard asks for a name and a category (picked from
qBittorrent's own list) and suggests the rest: the source folder from the category's save path, a destination
next to your other libraries, and the matching Jellyfin library. It checks the folders as you type and offers a
dry run. Periodica only reads from qBittorrent and Jellyfin — it never creates a category or a Jellyfin library.

- **Per library:** category, folders, Jellyfin library, title formats, language, cover width, format priority,
  allowed extra file types and *Delete after (days)*.
- **Shared:** the qBittorrent and Jellyfin connections, the scan API and key, the scan interval, and the
  automatic-delete switch, grace period and safety limit.
- **Kept apart:** each category and name is used once, and no library's folders may sit inside another's. A scan
  refreshes only the Jellyfin libraries that changed. Publications, Deletions and Unmatched gain a library
  filter once you have more than one.
- **Disable** stops scanning a library and protects it from automatic delete. **Delete** forgets it inside
  Periodica only; files and downloads stay, and adding it again picks them up without linking anything twice.

---

## Without qBittorrent or Jellyfin

**Without Jellyfin:** leave Settings → Jellyfin empty, or untick *Use Jellyfin*. New issues then appear after
Jellyfin's own scan — turn on real-time monitoring for the library, or rely on its schedule.

**Without qBittorrent (folder mode):** Settings → Download client → **None: watch the source folder**. Any
client or script can fill it.

- **Everything directly inside the source folder is one download**: a folder or a single file. The folder must
  hold nothing else. Hidden entries are skipped, symlinks are never followed, and a download with more than
  5000 files or nested deeper than 6 levels goes to Unmatched instead.
- **Finished** means nothing in it has changed for the **settle time** (default 5 minutes) and it holds no
  partial files (`.!qB`, `.part`, `.crdownload`, `.tmp`). In qBittorrent, *Append .!qB extension to incomplete
  files* makes a stalled download obvious; otherwise keep incomplete downloads outside the source folder.
- **Nothing is ever deleted**: both automatic and manual delete need qBittorrent. Remove old downloads
  yourself; their issues stay until you remove them on the publication page.
- **Scan API (optional):** give each library a category. A call must name one, and may send the download's path
  inside that library's source folder, so it is linked at once instead of after the settle time:
  ```bash
  curl -fsS -m 10 -X POST -H "X-Api-Key: <API-KEY>" --data-urlencode "category=%L" --data-urlencode "path=%F" http://<server-ip>:8765/api/v1/scan
  ```
  A path outside the source folder is ignored and logged.

Switching modes asks for confirmation with a dry run. Issues already linked stay where they are.

### Offline mode

For an install that makes no network connections at all, set this in the environment and redeploy:

```bash
OFFLINE_MODE=true
```

- **Locked off while it is set:** qBittorrent (the source folder is watched, as in folder mode), Jellyfin, and
  the scan API (`404`). The UI shows a banner and refuses to switch them on.
- **The web UI stays on**, so you can still see what was linked and change other settings.
- **Your settings are kept.** Remove the variable, redeploy, and the previous setup is back. Nothing is linked
  twice in either direction.
- **Nothing is deleted** while offline.

Periodica then opens no outgoing connection. For a guarantee at the network level, block the container's
outgoing traffic in your firewall; Docker's own networking is not affected by the variable.

---

## Updates

- **`main`** is always the latest release; **`develop`** collects changes until the next one.
- Every release has a tag, a GitHub Release with the notes from [CHANGELOG.md](../CHANGELOG.md), and images:

  | `IMAGE_TAG` | Meaning |
  |---|---|
  | `latest` / `stable` | newest release (default) |
  | `1.0` | newest 1.0.x: fixes only |
  | `1.0.0` | exactly this release |
  | `dev` | build from `develop`: **testing only** |
  | `sha-abc1234` | one exact build |

- **Updating:** redeploy with *re-pull images* (or `docker compose pull && docker compose up -d`). The sidebar
  shows the running version.
- **Before a database upgrade** the old database is copied to `CONFIG_PATH/periodica.db.bak-v<N>-<time>` (the
  newest 5 are kept). A version refuses to start on a database upgraded by a newer one, rather than damage it.

**Trying a development build:** disarm automatic delete first, set `IMAGE_TAG=dev`, redeploy, and test. Run
only one Periodica against the same qBittorrent and library at a time. To go back, set `IMAGE_TAG=latest`; if
the dev build upgraded the database and the release refuses to start, restore the newest
`periodica.db.bak-v*` as `periodica.db` with the container stopped.
