# Musicfetcherr

Musicfetcherr is a self-hosted Telegram bot and REST API for downloading, tagging,
and organising music from JioSaavn and YouTube. Paste a Spotify, YouTube, or
YouTube Music link, or search by song name. Files are saved under
`Artist/Album/Title`, with optional Plex and Navidrome library rescans.

**[Follow the detailed setup guide](docs/SETUP.md)** for Portainer, Docker Compose,
Telegram, Tailscale, storage, and troubleshooting.

## What it does

- **Links and playlists** try JioSaavn first, then YouTube Music. YouTube lists can also try the exact video
  in the playlist. Videos labelled as full movies are skipped. JioSaavn matching
  removes common upload credits and uses the available artist and duration metadata.
- **Spotify links** supply track and album metadata for matching against the audio
  sources. Spotify does not supply the downloaded audio.
- **Telegram** provides a source and quality picker for single tracks, playlist
  progress, a **Cancel playlist** button, the source of each saved track, and
  **Search YouTube** links for failed tracks.
- **Library handling** checks for existing songs by artist and title, adds available
  metadata, artwork, and LRCLIB lyrics, and requests Plex or Navidrome rescans when
  configured.
- **Networking** uses a Tailscale exit node in the bundled Docker stack.
  The app checks for Indian egress just before a JioSaavn download. Optional router
  forwarding uses UDP `53035` by default.
- **Optional FLAC support** uses a separately configured qBittorrent instance.
  The bundled stacks enable JioSaavn and YouTube Music.

## Documentation

| Guide | What it covers |
| --- | --- |
| [Setup](docs/SETUP.md) | Docker and Portainer, Tailscale, Telegram, storage, media libraries, updates, and diagnostics |
| [Native deployment](docs/deployment.md) | Python, local configuration, and optional systemd services |
| [Sources and matching](docs/engines.md) | Playlist matching, fallback order, source limits, and tagging |
| [REST API](docs/api.md) | Endpoints, asynchronous jobs, progress, and cancellation |
| [Optional qBittorrent setup](docs/flac-research.md) | FLAC configuration and shared storage requirements |

Musicfetcherr builds on [MusicFetch](https://github.com/AadiSPrabs/MusicFetch).
Use sources and media you are authorised to access. Third-party availability and
formats can change.
