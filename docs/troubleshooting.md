# Troubleshooting

## The qBittorrent connection

The **Test** button on Settings → Download client shows qBittorrent's HTTP status and reply.

- **Traffic comes from a Docker gateway, not the container's IP.** qBittorrent usually sees the gateway
  address, which makes an auth-bypass whitelist unreliable — use the username and password.
- **`HTTP 401` with no login entry in qBittorrent's log** means the request was rejected before login. Its
  *Execution log* usually says why:
  - *Invalid Host header*: qBittorrent → Options → Web UI → add that IP to **Server domains**, or turn off host
    header validation.
  - *Origin/Referer mismatch*: the URL in Periodica must use exactly the host and port qBittorrent is reached at.
- **`HTTP 403`**: no valid login, or the IP is banned after failed attempts (restart qBittorrent).

## A finished download doesn't trigger a scan

qBittorrent doesn't capture the output of *Run external program*, so a failed `curl` looks exactly like a
successful one. In this order:

1. qBittorrent → **Execution log** shows the command as it ran, with `%L` replaced by the category. An empty
   category means the torrent was added outside the RSS rule, and Periodica queues a plain scan instead. Check
   the address character by character — a typo just runs into the 10 second timeout.
2. Periodica → **System → Status** → *Scan API*: if it is off, every call gets `404`.
3. Periodica → **System → Logs** at the time of the download:
   - no `POST /api/v1/scan` line: the request never arrived (wrong address, or a firewall — see below);
   - a line with `401`: the key in the command isn't the current one (System → *Show API key*).
4. Run it by hand to see the real error:
   ```bash
   docker exec <qbittorrent-container> curl -fsS -m 10 -X POST -H "X-Api-Key: <API-KEY>" --data-urlencode "category=news" http://<server-ip>:8765/api/v1/scan
   ```
   `{"status":"scan queued"}` means it works; `{"status":"ignored"…}` means the category isn't any library's.

**qBittorrent behind a VPN container** (gluetun and similar) blocks traffic to the LAN, so the call times out.
Allow the one server address in that container's firewall settings, e.g.

```yaml
      - FIREWALL_OUTBOUND_SUBNETS=<server-ip>/32
```

## Jellyfin keeps showing deleted issues

Jellyfin skips a library whose root folder is empty, assuming an unmounted drive. Periodica keeps a hidden
`.periodica-library` file there so this never happens. If you deleted it, the next scan recreates it.

## Jellyfin doesn't show new issues

If the files are in the library folder and Library health is clean, but a Jellyfin *Books* library still
doesn't show them even after a scan in Jellyfin: remove that library in Jellyfin and add it again with the same
folder. Jellyfin has been seen to ignore new books in an existing library until then, and a scan of a single
library logs nothing in Jellyfin, so its log won't tell you.

## Links fail with "cross-device link"

Hardlinks only work inside one filesystem *and* one bind mount, which is why the compose file mounts a single
`DATA_PATH:/data`. Mounting `torrents` and `media` separately breaks every link. **System** checks this.

## Logs

- **Activity**: what Periodica did (linked, deleted, scans, warnings), kept 30 days in the database.
- **System → Logs**: the technical log, live, with a Pause button. Held in memory, so a restart clears it.
- **Log files**: `CONFIG_PATH/logs/periodica.log` and its rotated copies survive restarts. Size and count are
  set under **Settings → Logging** (default 5 MB × 5), and each file can be downloaded from the Logs page. Set
  the size to `0` to write no files at all — useful to let a disk sleep or spare an SSD. `docker logs` still
  shows the same lines unless you change the stack's logging driver.
- **Log level**: *Info* by default (**Settings → Logging**). *Debug for 30 min* on the Logs page turns on
  per-file and per-API-call detail for one troubleshooting session, then expires. `LOG_LEVEL` in the
  environment only seeds the initial value.
