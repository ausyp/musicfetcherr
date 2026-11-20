# Deployment

Musicfetcherr runs as two long-lived processes:

1. **API** — uvicorn serving `api:app` on `127.0.0.1:8090`
2. **Bot** — `tools/tgbot.py` long-polling Telegram

Both are plain `systemd` units in this project (files below). The design
assumption: the REST API binds **localhost only** — put it behind a reverse
proxy (Caddy/nginx/Cloudflare Tunnel) if you want it reachable from outside.

## systemd units

`/etc/systemd/system/musicfetcherr-api.service`:

```ini
[Unit]
Description=Musicfetcherr download API (uvicorn, 127.0.0.1:8090)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/musicfetcherr
ExecStart=/opt/musicfetcherr/.venv/bin/uvicorn api:app --host 127.0.0.1 --port 8090
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/musicfetcherr-bot.service`:

```ini
[Unit]
Description=Musicfetcherr Telegram bot
After=network-online.target musicfetcherr-api.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/musicfetcherr
ExecStart=/opt/musicfetcherr/.venv/bin/python tools/tgbot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Install and enable:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now musicfetcherr-api musicfetcherr-bot
sudo systemctl status musicfetcherr-api musicfetcherr-bot
```

The bot unit declares `After=musicfetcherr-api.service` so it doesn't race the
API on boot.

## Directory layout (this install)

```
/opt/musicfetcherr/          # the repo
├── config.yaml           # real config (gitignored; start from config.example.yaml)
├── staging/              # engine download scratch space (gitignored)
└── .venv/                # virtualenv (gitignored)

/mnt/hdd/media/music2test # output.root — the Jellyfin-scanned library
```

`staging/` lives on the root filesystem while the library is on a separate
mount. That is deliberate and handled: the final move uses `shutil.move`
(copy + delete across devices) rather than `os.replace`, which would raise
`EXDEV` on cross-device links. Don't "optimize" that to a rename.

## Dependencies

- Python 3.11+ (developed on 3.13)
- `ffmpeg` on PATH — used to convert WebP cover art to JPEG for embedding
  (YouTube thumbnails arrive as WebP; MP4 `covr` atoms must carry real
  image formats)
- Network access to: `www.jiosaavn.com`, `music.youtube.com` /
  `www.youtube.com`, `musicbrainz.org`, `coverartarchive.org`,
  `lrclib.net`, `open.spotify.com`, `api.telegram.org`

Everything is in `requirements.txt`; the Telegram bot uses only the Python
stdlib for transport (urllib long-poll) — no bot framework needed.

## Operations notes

- **Queue is in-memory** — restarting the API drops queued/running jobs.
  Jobs are short-lived (a song takes seconds to a couple of minutes), so
  this is a non-issue in practice.
- **Source liveness changes monthly** — JioSaavn/YouTube endpoints can
  break without warning. If downloads start failing, check
  `journalctl -u musicfetcherr-api -f` first; the engines log the failure
  stage (search vs download vs which quality/player client).
- **`config.yaml` contains secrets** (bot token, optional API key). It is
  gitignored — never commit it.
- **Ownership**: bot messages are refused for chat IDs outside
  `telegram_bot.allowed_chats`. Add your own chat ID there or the bot will
  answer "Not authorized."
