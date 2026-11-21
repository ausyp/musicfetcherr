# Optional qBittorrent setup

The qBittorrent adapter can download FLAC from a compatible torrent pack.
The bundled Portainer stacks do not start a qBittorrent service or enable
this adapter. Configure it in a [native installation](deployment.md), or
provide the equivalent service settings and mounts in a custom Compose file.

1. Configure a reachable qBittorrent Web UI. Keep it on a trusted network and
   configure its credentials privately.
2. Enable `qbit` in `engine.order` in `config.yaml`. Supply the Web UI URL,
   username, and password in the `qbit` section.
3. Give Musicfetcherr access to the qBittorrent download directory at the
   same absolute path returned by the client. Give Musicfetcherr write
   permission to both that location and its staging directory. Place staging
   on the same filesystem as the qBittorrent download directory, since the
   adapter renames the completed file into staging.
4. Restart the API and check that it can search and list qBittorrent
   candidates. A candidate may describe a pack. The adapter selects a FLAC
   file and checks duration against the reference when available.

```yaml
engine:
  order: [qbit, jiosaavn, youtube_music]

qbit:
  url: http://127.0.0.1:8080
  username: YOUR_WEBUI_USERNAME
  password: YOUR_WEBUI_PASSWORD
  category: musicfetcherr
  staging_dir: /srv/downloads/musicfetcherr-staging
```

A pack can fail when file listings are unavailable, peers are unreachable, or
the title or duration does not match. The adapter uses its configured category
to manage the torrent and can remove it after processing. Use a separate
category for unrelated downloads.
