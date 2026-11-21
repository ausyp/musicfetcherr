# Musicfetcherr setup guide

Musicfetcherr runs as a Docker Compose stack with two containers. The
`musicfetcherr` app handles Telegram, the local API, downloads, and library scans.
`musicfetcherr-tailscale` joins your tailnet and provides the outbound proxy
the app uses for downloads. Plex, Navidrome, and qBittorrent run separately.

## 1. Prepare the Docker host and music storage

You need Linux with Docker Engine and Docker Compose v2, access to a Tailscale
exit node, and a Telegram account. Portainer is optional. If you use it, select
a Docker Standalone environment. The supplied Compose files build the app locally.

```sh
docker version
docker compose version
id
```

Note the UID and GID of the Linux account that should own new files. Set them
using `PUID` and `PGID`.

| Storage | Compose file | Settings |
| --- | --- | --- |
| Local directory or a share already mounted on the Docker host | `docker-compose.yml` | `MUSIC_PATH` is the absolute host path |
| Existing named Docker volume, including a NAS share configured as a volume | `docker-compose.volume.yml` | `MUSIC_VOLUME` is its exact Docker name. `MUSIC_SUBDIR` is optional |

For a new local directory, create it with the chosen owner. Replace the example
IDs and path as needed. For an existing directory or network share, verify write
permissions for the chosen UID and GID and read access for the media server.

```sh
sudo install -d -o 1000 -g 1000 /srv/music
```

For the existing-volume variant, use `docker volume ls` or Portainer’s **Volumes**
page to find the name, including any stack prefix. If the volume is SMB/CIFS,
the share credentials and mount options must allow the selected UID and GID
to write files.

## 2. Prepare the Tailscale exit node

1. Install Tailscale on the exit-node device and join the same tailnet as
   Musicfetcherr. The supplied stack expects an exit node with public egress in
   India for JioSaavn. For Linux, enable IP forwarding and advertise the node as
   described in [Tailscale’s exit-node guide](https://tailscale.com/docs/features/exit-nodes/how-to/setup).
2. In the Tailscale admin console, open **Machines → your exit node → Edit route
   settings**, approve **Use as exit node**, and copy its Tailscale IP into
   `TS_EXIT_NODE`. If your tailnet uses a custom access policy, allow the
   Musicfetcherr device to use the exit node.
3. Generate an auth key for the sidecar from the admin console’s **Keys** page.
   Use a key that can register a persistent device and put it into `TS_AUTHKEY`.
   Once the container starts, verify that `musicfetcherr` appears in **Machines**.
   Keep the `tailscale-state` volume across redeploys.

The watchdog checks exit-node reachability. With `EXIT_NODE_FALLBACK=true`, it
can switch the download proxy to the connection of the Docker host during an
outage and reselect the exit node after recovery. Switches appear in the app
logs. JioSaavn separately verifies the proxy’s public IP country immediately
before its download attempt. If verification fails, the bot includes the reason
in the result. Telegram, Spotify link lookups, and the main metadata services
are excluded from the download proxy in the supplied Compose files.

## 3. Create and allowlist your Telegram bot

1. Open [BotFather](https://t.me/BotFather), send `/newbot`, and follow its
   prompts. Put the issued token in `TELEGRAM_BOT_TOKEN`.
2. Set `TELEGRAM_ALLOWED_CHATS` to the numeric chat IDs that can use the bot,
   separated by commas. For a private chat, use your Telegram user ID. If you
   don’t know it, start the bot with the allowlist blank, send `/start`, and
   look for the rejected chat’s ID in the app logs. Add the ID and redeploy.
3. Open the bot in Telegram and press **Start**. Keep its token, chat IDs,
   and all other credentials in your private deployment settings.

## 4. Deploy Musicfetcherr

Start with [stack.env.example](../stack.env.example). Fill in `TS_AUTHKEY`,
`TS_EXIT_NODE`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_ALLOWED_CHATS`. Choose
the matching storage settings from step 1.

### Portainer

1. In your Docker Standalone environment, go to **Stacks → Add stack**.
2. Name it `musicfetcherr` and choose **Repository**. Paste the repository or
   fork URL. Use `refs/heads/main` as the repository reference and the Compose
   path from step 1.
3. Under **Environment variables**, enter the filled-in settings. Use the
   private-repository authentication option only if the source requires it.
4. Deploy the stack. Once the build completes, check that
   `musicfetcherr-tailscale` is healthy and `musicfetcherr` is running.
   See [Portainer’s stack documentation](https://docs.portainer.io/user/docker/stacks/add)
   for the repository and environment controls.

### Docker Compose CLI

Clone the repository or fork to the Docker host and run these commands from
the checkout. For the existing named volume, add
`-f docker-compose.volume.yml` to the Compose command.

```sh
cp stack.env.example stack.env
chmod 600 stack.env
# Fill in stack.env before running this command.
docker compose --env-file stack.env up -d --build
```

## 5. Connect Plex or Navidrome (optional)

Each media server must be able to read the directory or share used by
Musicfetcherr. If the media server runs in its own container, add the shared
music mount to that container as well.

| Setting | Value |
| --- | --- |
| `PLEX_URL` | Plex address reachable by the app. The default is `http://host.docker.internal:32400` |
| `PLEX_TOKEN` | Plex API token for the server. A blank value disables Plex scan requests |
| `PLEX_MUSIC_PATH` | Optional library root as Plex sees it. Set this for album-level refreshes |
| `NAVIDROME_URL` | URL of an existing Navidrome instance, reachable by the app |
| `NAVIDROME_USER`, `NAVIDROME_PASSWORD` | Navidrome account permitted to start library scans |

Add the shared folder to a Plex Music library. Enable **Prefer local metadata**
if you want Plex to prioritise the tags written into the files. For token
retrieval, see [Plex’s token guide](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).
For another host, use its reachable LAN address or DNS name. A server on the
same Docker host must listen on a published or host-reachable port for
`host.docker.internal` to work.

## 6. Verify the first download

1. Open **Containers → musicfetcherr → Logs** in Portainer. Look for the writable
   music-folder check, the Uvicorn startup line, and the bot coming online.
2. Send `/start` and `/status` in Telegram. Search for a known song with
   `search <song name>`, or send a single song link before trying a long playlist.
3. Choose a source when the bot offers options. Check the delivered-source label
   and look for the saved file under `Artist/Album/Title.m4a` or `.flac`.
4. If a media server is configured, verify the file appears in its music library.

Spotify and YouTube links and playlists try JioSaavn first, then YouTube Music.
YouTube lists can also fall back to the original video. The bot reports the source of each saved track and gives failed tracks
a **Search YouTube** link. Entries labelled as full movies are skipped. Songs
already recognised in the library are reported as **already had**. For details,
see [sources and matching](engines.md).

Use **Cancel playlist** on its queue or progress message. A queued playlist
cancels immediately. A running playlist stops after its current operation
returns. The bot reports any tracks already saved.

## 7. Optional direct Tailscale connection

The stack publishes UDP `53035` on the host and uses the same UDP port inside
the Tailscale container. If it conflicts with another service, change
`TS_UDP_PORT` in the stack settings and redeploy.

| Router field | Value |
| --- | --- |
| Protocol | UDP |
| External port | `53035`, or the value of `TS_UDP_PORT` |
| Internal IP | LAN IP of the machine or LXC that runs Docker |
| Internal port | Same as the external port |

Reserve the Docker host’s LAN IP. Create the rule on the internet-facing router.
If you forward at the exit-node end instead, use that device’s LAN IP and its
own Tailscale UDP port. If the WAN is behind ISP CGNAT, try the end with a public
WAN address or ask the ISP about a public IP. If you control an upstream NAT
router, configure the matching rule there.

Run these checks, replacing the example port and exit-node value as needed.

```sh
# On the Docker host
docker port musicfetcherr-tailscale 53035/udp

# In the musicfetcherr-tailscale container console
tailscale status
EXIT_NODE_IP=100.x.y.z  # Replace with your exit node’s Tailscale IP.
tailscale ping "$EXIT_NODE_IP"
tailscale netcheck
```

A ping by IP and port indicates a direct connection. DERP replies indicate a
relay path. While a download is active, look for `direct` and increasing
receive bytes on the exit-node line in `tailscale status`. See
[Tailscale’s connection guide](https://tailscale.com/docs/reference/connection-types).

## 8. Update and troubleshoot

For a Portainer repository stack, open **Stacks → musicfetcherr → Pull and
redeploy**. For a CLI deployment, update the checkout and repeat the Compose
command from step 4. Keep the persistent volumes and environment settings.
Job state is in memory, so wait for downloads to finish if you need their results.

| Symptom | Check |
| --- | --- |
| Tailscale container never becomes healthy | Verify the auth key, device approval, exit-node address, and the sidecar logs |
| Telegram says **Not authorized** | Check the numeric ID and the comma-separated allowlist |
| Bot does not respond | Check that the app and Tailscale containers are running and that the bot token is valid |
| Songs save but do not appear in Plex or Navidrome | Verify the media server uses the same storage and check scan credentials and logs |
| JioSaavn reports no match | Try the soundtrack title in the bot. Differences in transliteration, duration, and upload metadata can prevent matching |
| JioSaavn is skipped because egress cannot be verified | Run the country check below in the app container |
| YouTube says sign-in required, format unavailable, or video removed | Check the individual attempt. A relay or direct connection alone does not resolve a provider-side restriction |
| Music directory permission denied | Check `PUID`, `PGID`, and the host share or volume write permissions |
| `404` for an old job ID | Use the current run’s job list. A restart clears the in-memory queue |

```sh
# In the musicfetcherr app container console
python -c 'from egress import lookup_country; print(lookup_country() or "unknown")'
python -c 'from urllib.request import urlopen; print(urlopen("http://127.0.0.1:8090/health", timeout=5).read().decode())'
```

See the [REST API reference](api.md) for querying jobs and the
[native deployment guide](deployment.md) for running outside Docker.

## Stack settings reference

| Variable | Purpose |
| --- | --- |
| `TS_AUTHKEY`, `TS_EXIT_NODE` | Tailscale registration key and exit-node Tailscale IP |
| `TS_UDP_PORT` | Published Tailscale UDP port. Defaults to `53035` |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHATS` | Bot token and comma-separated chat allowlist |
| `MUSIC_PATH` | Bind-mount variant library path. Defaults to `/srv/music` |
| `MUSIC_VOLUME`, `MUSIC_SUBDIR` | Existing volume name and optional library subdirectory |
| `PUID`, `PGID` | File owner and group IDs. Both default to `1000` |
| `PLEX_URL`, `PLEX_TOKEN`, `PLEX_MUSIC_PATH` | Optional Plex scan configuration |
| `NAVIDROME_URL`, `NAVIDROME_USER`, `NAVIDROME_PASSWORD` | Optional Navidrome scan configuration |
| `EXIT_NODE_FALLBACK` | Use the host connection during exit-node outages. Defaults to `true` |
| `JIOSAAVN_WHEN_FALLBACK` | Allow JioSaavn without its India egress check. Defaults to `false` |
| `NO_PROXY_EXTRA` | Additional comma-separated entries for the download proxy bypass list |
| `TZ` | Container time zone. Defaults to `Etc/UTC` |

The bundled Compose files pass the settings above into the stack. The native
configuration also exposes options for duplicate checking, provider selection,
and optional qBittorrent integration. See [config.example.yaml](../config.example.yaml).
