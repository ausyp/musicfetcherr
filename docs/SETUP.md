# Musicfetcherr – Detailed Setup Guide

## Overview

Send a Spotify, YouTube or YouTube Music link (or `search <song>`) to your Telegram bot. The song is downloaded, tagged and saved into your music folder. Then **Plex** and/or your existing **Navidrome** server rescans, so it appears in Plexamp or any Subsonic app (Narjo, play:Sub, Amperfy, Symfonium). Setup takes about 45 minutes.

The stack is 2 containers:

| Container | What it does | Network |
| --- | --- | --- |
| `musicfetcherr-tailscale` | Joins your tailnet and runs a local proxy that sends download traffic out through an exit node in India. Needs no TUN device, so it works in a Proxmox LXC or on a NAS | India exit node (or your normal connection on fallback) |
| `musicfetcherr` | Telegram bot, downloader (JioSaavn 320 kbps, YouTube Music ~128 kbps), exit-node watchdog, Plex + Navidrome rescan | Shares the Tailscale container's network |

Navidrome is **not** part of the stack. If you already run one, the bot can tell it to rescan (Step 5).

```
Telegram ─► musicfetcherr ──► tailscale ──► India exit node ──► JioSaavn / YouTube Music
                 │                 └─(India down)─► normal connection ──► YouTube Music
                 ├─► music folder/Artist/Album/Song.m4a ◄── Plex ──► Plexamp
                 │                                      ◄── Navidrome (optional) ──► Subsonic apps
                 └─► "rescan" ──► Plex and/or Navidrome
```

Why India? JioSaavn (the 320 kbps source) only works from an Indian IP address. Telegram, Spotify link lookups, MusicBrainz, cover art and lyrics don't need it and go direct.

If the India exit node goes down, downloads switch to your normal connection, JioSaavn pauses, and YouTube Music takes over until India is back.

**Checklist before you start**

- [ ] A Linux server with Docker and Portainer (Step 1)
- [ ] A Tailscale account with a device in India set up as an exit node (Step 2)
- [ ] A Telegram account (Step 3)
- [ ] Plex with a music library (Step 4) and/or an existing Navidrome server (Step 5) — both optional
- [ ] About 2 GB of free disk for the image, plus space for music

## Step 1: Prepare the server

The server needs Docker, Portainer, somewhere to save music, and a user ID that can write there.

1. **Check Docker** over SSH: `docker version` and `docker compose version`. Both should print versions.
2. **Install Portainer** if you don't have it:
   - `docker volume create portainer_data`
   - `docker run -d -p 9443:9443 --name portainer --restart=always -v /var/run/docker.sock:/var/run/docker.sock -v portainer_data:/data portainer/portainer-ce:latest`
   - Open `https://<server-ip>:9443` and create the admin user.
3. **Choose where songs are saved.** Either:
   - **A folder on the server** (or your existing Plex music folder): `sudo mkdir -p /srv/music`. You'll use `MUSIC_PATH` and the compose file `docker-compose.yml`.
   - **An existing Docker volume**, such as an SMB/NAS share added in Portainer → *Volumes* (e.g. a volume `nas-media` for `smb://<nas-ip>/media`). You'll use `MUSIC_VOLUME` (the volume's exact name in Portainer), `MUSIC_SUBDIR` (a folder inside it, e.g. `music`) and the compose file `docker-compose.volume.yml`. Portainer shows the volume's mount point as `/var/lib/docker/volumes/<name>/_data`; `<name>` is what goes in `MUSIC_VOLUME`.
4. **Find your user and group IDs**: run `id`. Note the numbers after `uid=` and `gid=` (often `1000` and `1000`). These are `PUID` and `PGID`.
5. **Make sure that user can write there.**
   - Server folder: `sudo chown -R 1000:1000 /srv/music` (use your own numbers).
   - SMB volume: its mount options must allow writing, e.g. `uid=1000,gid=1000` (matching `PUID`/`PGID`) or `file_mode=0777,dir_mode=0777`, plus a share user with write access. Portainer → *Volumes* → your volume shows its options; if they're wrong, recreate the volume with these added.

## Step 2: Tailscale

You need the exit node's Tailscale IP and an auth key for the new container.

1. **Set up the exit node in India.** On a device there (a PC, Raspberry Pi or cloud server) install Tailscale and run `sudo tailscale set --advertise-exit-node`. Then open [login.tailscale.com/admin/machines](https://login.tailscale.com/admin/machines), find the device, open its menu → *Edit route settings*, and tick **Use as exit node**.
2. **Copy its Tailscale IP** (starts with `100.`) from the Machines list. This is `TS_EXIT_NODE`.
3. **Check your access rules.** If you never edited *Access controls*, skip this. If you did, allow `autogroup:internet`, for example a rule with `"dst": ["autogroup:internet:*"]`.
4. **Create an auth key**: *Settings → Keys → Generate auth key*. Turn on **Reusable**; leave *Ephemeral* off. Copy the `tskey-auth-…` value. This is `TS_AUTHKEY`.
5. **After the first deploy** (Step 6), find the new device `musicfetcherr` in Machines and choose **Disable key expiry**. Otherwise it drops off the tailnet when its key expires (180 days by default).

The auth key itself expires after at most 90 days, but that only matters if you delete the stack's `tailscale-state` volume and it has to log in again.

For faster downloads, see [Faster downloads: a direct connection](#faster-downloads-a-direct-connection) once everything works.

## Step 3: Telegram bot

You need a bot token and the chat ID of everyone allowed to use it.

1. In Telegram, open **@BotFather** and send `/newbot`.
2. Give it a name and a username ending in `bot`.
3. Copy the token it replies with (looks like `123456789:AAH…`). This is `TELEGRAM_BOT_TOKEN`. Keep it secret.
4. Open **@userinfobot** and send it any message. Copy the number after *Id*. This is your chat ID.
5. For other people, have each of them message @userinfobot and send you their ID. Put all IDs in `TELEGRAM_ALLOWED_CHATS`, comma-separated (e.g. `111111111,222222222`). Anyone not on the list gets "Not authorized".
6. Open your new bot and press **Start** so it can message you.

## Step 4: Plex (optional)

Plex must see the same music folder, and the bot needs your Plex token. No Plex? Leave `PLEX_TOKEN` blank and skip this step.

1. **Add the folder to a music library.** Plex Web → *Settings → Manage → Libraries* → your *Music* library (or *Add Library → Music*) → *Add folders* → the music folder. If Plex runs in Docker, mount the same folder or share into the Plex container first.
2. **Turn on local metadata.** Same library → *Edit → Advanced* → tick **Prefer local metadata**. Plex then keeps the titles, artists and albums the bot writes into each file instead of replacing them with its own (often wrong) matches.
3. **Get your Plex token.** In Plex Web, open any track → *⋯ → Get Info → View XML*. In the address bar of the page that opens, copy the value after `X-Plex-Token=`. This is `PLEX_TOKEN`.
4. **Choose `PLEX_URL`.** Same-server and home-network addresses are reached directly; only download traffic goes through India.

| Where Plex runs | `PLEX_URL` |
| --- | --- |
| Same server (directly, in Docker with port 32400 published, or in the same LXC) | leave the default `http://host.docker.internal:32400` |
| Another machine on your home network | `http://192.168.x.x:32400` |
| Another machine on your tailnet | `http://<its 100.x Tailscale IP>:32400` |

5. **Optional: faster scans.** Set `PLEX_MUSIC_PATH` to the music folder *as Plex sees it* (e.g. `/srv/music`, or `/data/music` inside a Plex container). Then Plex only rescans the new album folders instead of the whole library.

## Step 5: Navidrome (optional)

The stack doesn't include Navidrome. If you already run one, give the bot its address and an admin login so it triggers a rescan after each download. No Navidrome? Leave these blank and skip this step.

1. **Make sure Navidrome reads the same music**: its music folder must be the same folder or share the bot saves into.
2. **`NAVIDROME_URL`** — how the bot reaches it:

| Where Navidrome runs | `NAVIDROME_URL` |
| --- | --- |
| This server (port 4533 published) | `http://host.docker.internal:4533` |
| Another machine on your home network | `http://192.168.x.x:4533` |
| Another machine on your tailnet | `http://<its 100.x Tailscale IP>:4533` |
| Public address (reverse proxy) | `https://music.example.com` |

3. **`NAVIDROME_USER` / `NAVIDROME_PASSWORD`** — an admin account on that Navidrome (only admins can start a scan).

Navidrome uses only the tags in your files, so titles and artists always show as downloaded. It also rescans on its own schedule, so new songs appear eventually even without this.

## Step 6: Deploy in Portainer

Portainer pulls the repo, builds the image, and starts both containers with your settings.

1. Portainer → your environment (usually *local*) → **Stacks → + Add stack**.
2. Name: `musicfetcherr`. Build method: **Repository**.
3. Repository URL: this repository's GitHub URL (or your fork's). Reference: `refs/heads/main`. Compose path: `docker-compose.yml` for a server folder, or `docker-compose.volume.yml` for an existing Docker/SMB volume.
4. If the repo is private, turn on **Authentication**: your GitHub username, and as password a GitHub fine-grained token with read-only *Contents* access to the repo.
5. Optional: turn on **GitOps updates** (polling, e.g. every 24h) so changes pushed to the repo redeploy automatically.
6. **Environment variables → Advanced mode**, paste this and fill in your values (also in `stack.env.example`):

```
TS_AUTHKEY=tskey-auth-xxxxxxxxxxxx
TS_EXIT_NODE=100.x.y.z
TELEGRAM_BOT_TOKEN=123456789:AAH...
TELEGRAM_ALLOWED_CHATS=111111111
PLEX_TOKEN=
PLEX_MUSIC_PATH=
NAVIDROME_URL=
NAVIDROME_USER=
NAVIDROME_PASSWORD=
MUSIC_PATH=/srv/music
# or, with docker-compose.volume.yml instead of MUSIC_PATH:
# MUSIC_VOLUME=nas-media
# MUSIC_SUBDIR=music
PUID=1000
PGID=1000
EXIT_NODE_FALLBACK=true
TZ=Etc/UTC
```

7. Click **Deploy the stack**. The first build downloads Python, ffmpeg and Node and takes 3–10 minutes; Portainer may look stuck meanwhile. The `musicfetcherr` image is always built from the repo, never pulled from Docker Hub.
8. When it finishes, *Containers* should list `musicfetcherr-tailscale` (healthy) and `musicfetcherr` (running).
9. Do Step 2.5 now: disable key expiry on the new `musicfetcherr` device.

## Step 7: Verify it works

1. **Logs.** Portainer → *Containers → musicfetcherr → Logs*. Within a minute or two you should see:
   - `[entrypoint] music folder /music… is writable`
   - `[entrypoint] yt-dlp up to date (…)`
   - `[watchdog] watching exit node 100.x.y.z (fallback ON, every 30s)`
   - `Uvicorn running on http://127.0.0.1:8090`
   - `bot online: @your_bot`
2. **India routing.** Musicfetcherr verifies the public IP country when it is about to download from JioSaavn. The bot includes any routing issue in the download result. Tailscale still checks whether the exit node responds so it can switch connections automatically. Those route changes appear in the container logs.
3. **First download.** Send the bot a song link from Spotify or YouTube Music. It offers source buttons such as `[JS 320]` and `[YT 128]`; pick JS 320.
4. **File check.** The bot replies with the file, and it appears in your music folder as `Artist/Album/Song.m4a`.
5. **Players.** The log shows `plex refresh started` / `navidrome scan started` if those are set up. Within a minute the song appears in Plexamp (Recently Added) or your Navidrome app.
6. **Test the fallback (optional).** Stop Tailscale on the exit node for about three minutes. Check the container logs for the switch to your normal connection. A JioSaavn download should report that it was skipped and show the YouTube Music fallback outcome. Restart the exit node and check the logs for recovery.

## Faster downloads: a direct connection

Tailscale uses a direct UDP connection when it can and a relay when it needs one.
A reachable UDP port on either end can help establish the direct connection.

**Forward to the Musicfetcherr Docker host**

1. Deploy the updated stack. Both Compose variants publish UDP `53035` and set
   the Tailscale sidecar to listen on that port. If the host already uses it,
   set `TS_UDP_PORT` to a free UDP port in Portainer, then redeploy.
2. Use the LAN IP of the machine or LXC running Docker. Reserve that IP in your router.
3. On the router facing the internet, forward **UDP 53035** externally to
   **UDP 53035** on that LAN IP. If you set `TS_UDP_PORT`, use it for both
   ports. On ASUS, the setting is under **WAN → Virtual Server / Port Forwarding**.
4. From the Docker host, run `docker port musicfetcherr-tailscale 53035/udp`
   to verify the published port. From the Tailscale container console, run
   `tailscale ping 100.x.y.z` with the exit node’s Tailscale IP. The first
   replies may go through DERP. A reply via an IP and UDP port confirms a direct path.

**Forward at the India exit node instead**

Use the exit node device’s LAN IP and the UDP port its own `tailscaled`
process listens on, commonly `41641` on Linux. If the exit node runs in Docker,
publish its UDP port on that host as well.

If a router’s WAN IP is in `100.64.0.0/10` or a private IPv4 range, it has
upstream NAT. Forward at the upstream router if you control it. With ISP CGNAT,
use the other end if it has a public WAN IP, or ask the ISP for a public IPv4 address.

## Everyday use, updates and settings

**Bot commands**

- Paste a link — a single track asks which source; an album or playlist downloads everything, with a live progress message (e.g. `▓▓▓▓░░░░ 12/40 · ✅ 9 · ⏭ 3 already had`). Long playlists are fine: the bot waits as long as it takes.
- Tap **Cancel playlist** on its queue or progress message. A queued playlist stops immediately. A running playlist finishes the current track operation and skips the remaining tracks. Songs already saved stay in your library.
- `search <song or artist>` — tap a result to download it.
- `/status` — what's in the queue. `/help` — the command list.

**Spotify playlists: first 100 songs only.** The bot reads Spotify playlists from Spotify's public player, which never lists more than 100 songs. For a longer playlist the bot warns you and downloads the first 100. To get the rest, split the playlist into parts of 100 or fewer, or convert it to a YouTube Music playlist (e.g. with TuneMyMusic or Soundiiz) and send that link — YouTube playlists have no limit. Albums aren't affected.

**Duplicates**: songs you already have (same title by the same artist, in any album folder and any format) are skipped and reported as "already had", so you can re-send a playlist to pick up just the new songs. Album and artist folders that differ only in punctuation or soundtrack wording ("Interstellar" vs "Interstellar (Original Motion Picture Soundtrack)", "R.E.M." vs "R. E. M.") are reused instead of creating a second folder. Different recordings with the same name, like a "(Live)" or "(Female Version)", are kept.

**Change a setting**: Portainer → *Stacks → musicfetcherr → Editor* → edit the environment variables → **Update the stack**.

**Update to the latest code**: *Stacks → musicfetcherr* → **Pull and redeploy**. The image is rebuilt from the repo every time; *Re-pull image* isn't needed (it's safe either way).

**yt-dlp** (the YouTube downloader) updates itself every time the container starts. If YouTube downloads start failing, just restart the `musicfetcherr` container.

**All settings**

| Variable | Default | Effect |
| --- | --- | --- |
| `TS_AUTHKEY`, `TS_EXIT_NODE` | — | Tailscale key + exit node IP (required) |
| `TS_UDP_PORT` | `53035` | Tailscale UDP listen port, published on the Docker host for port forwarding |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHATS` | — | Your bot + who may use it (required) |
| `MUSIC_PATH` | `/srv/music` | Server folder for songs (`docker-compose.yml`) |
| `MUSIC_VOLUME`, `MUSIC_SUBDIR` | — | Existing Docker volume + folder inside it (`docker-compose.volume.yml`) |
| `PUID`, `PGID` | `1000` | User/group that writes the files |
| `PLEX_TOKEN` | — | Blank = no Plex rescan |
| `PLEX_URL` | `http://host.docker.internal:32400` | Where Plex is (Step 4) |
| `PLEX_MUSIC_PATH` | — | Music folder as Plex sees it, for per-album scans |
| `NAVIDROME_URL`, `NAVIDROME_USER`, `NAVIDROME_PASSWORD` | — | Your existing Navidrome; blank = skip |
| `EXIT_NODE_FALLBACK` | `true` | Exit node down → use your normal connection (JioSaavn paused). `false` = wait for the exit node |
| `JIOSAAVN_WHEN_FALLBACK` | `false` | `true` keeps JioSaavn on the fallback connection (needs JioSaavn Pro) |
| `SKIP_EXISTING` | `true` | `false` re-downloads songs you already have (saved as `Song (2).m4a`) |
| `YTDLP_AUTO_UPDATE` | `true` | `false` stops yt-dlp updating itself on start |
| `TZ` | `Etc/UTC` | Time zone for logs, e.g. `Europe/London` |

## Troubleshooting

| Problem | Likely cause | Fix |
| --- | --- | --- |
| `musicfetcherr-tailscale` never turns healthy | Bad or expired `TS_AUTHKEY` | New reusable key; redeploy |
| JioSaavn reports a non-India connection or an unverified country | Exit node is unavailable, or the country lookup failed | Check the exit-node logs and Step 2.1 and 2.3 |
| `bot exited … restarting container` | Wrong `TELEGRAM_BOT_TOKEN` | Copy the token again from @BotFather |
| Bot replies "Not authorized" | Your chat ID isn't in `TELEGRAM_ALLOWED_CHATS` | Add it and update the stack |
| Bot says `The read operation timed out`, downloads slow | Connection to the exit node is relayed (`via DERP`) | [Faster downloads](#faster-downloads-a-direct-connection) |
| Spotify playlist stops at 100 songs | Spotify's public player only lists 100 | Split the playlist, or send a YouTube Music copy |
| `WARNING: user … cannot write to /music…` or `Permission denied` | Music folder not writable by `PUID:PGID` | Server folder: `sudo chown -R 1000:1000 /srv/music`. SMB volume: mount options `uid=`/`gid=` or `file_mode=0777,dir_mode=0777` (Step 1.5) |
| `MUSIC_VOLUME is missing a value` on deploy | Using `docker-compose.volume.yml` without `MUSIC_VOLUME` | Add `MUSIC_VOLUME=<volume name>` |
| `external volume … not found` on deploy | Volume name doesn't match Portainer's | Copy the exact name from Portainer → *Volumes* (it may have a stack prefix, e.g. `mystack_nas-media`) |
| `pull access denied for musicfetcherr` | Stack file from an old version | Pull and redeploy once; the image is now always built, never pulled |
| `plex refresh failed` | Wrong `PLEX_TOKEN` or unreachable `PLEX_URL` | Step 4.3 and 4.4 |
| `navidrome scan refused` | Wrong `NAVIDROME_USER`/`PASSWORD`, or the user isn't an admin | Step 5.3 |
| Songs saved but not in Plexamp | Folder not in the Plex music library, or wrong `PLEX_MUSIC_PATH` | Step 4.1; clear `PLEX_MUSIC_PATH` to rescan the whole library |
| YouTube downloads fail with 403 | Old yt-dlp | Restart the `musicfetcherr` container (it updates yt-dlp on start) |
| Wrong titles in Plex | Prefer local metadata off | Step 4.2, then *Refresh Metadata* on the library |

**Known limits**

- One container runs the bot, downloader and watchdog, so a bot crash restarts all three and cancels a download in progress.
- Download traffic goes through a local proxy rather than a VPN tunnel. Everything in the app uses it, but a library that ignored proxy settings would connect from your normal connection instead.
- On the fallback connection, songs come from YouTube Music at ~128 kbps instead of 320 kbps.
- Spotify playlists are limited to their first 100 songs (see above).
- Downloading from YouTube and using JioSaavn through an Indian IP break those services' terms. Keep it to personal use.
