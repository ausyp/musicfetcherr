# Native deployment

For Docker or Portainer, use the [main setup guide](SETUP.md). In a native
installation, the API and Telegram bot run as separate processes. The native
installation uses the host’s networking unless you configure a route or proxy
for the processes.

## Install and verify

1. Install Python 3.11 or later, ffmpeg, and a JavaScript runtime supported by
   yt-dlp. Musicfetcherr explicitly enables Node for YouTube downloads when
   it is available on PATH. Clone this repository or your fork.
2. From the checkout, create a virtual environment and configure the app.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt 'yt-dlp[default]'
cp config.example.yaml config.yaml
chmod 600 config.yaml
```

3. Set `output.root` to an absolute, writable music directory. Configure
   `telegram_bot.token` and `telegram_bot.allowed_chats`. Set writable staging
   paths. The API listens on localhost by default. The bundled bot expects a
   loopback API without an API key. API-only clients can use an optional key
   as described in the [API reference](api.md).
4. Start the API in one terminal.

```sh
.venv/bin/uvicorn api:app --host 127.0.0.1 --port 8090
```

Start the bot in a second terminal from the same checkout.

```sh
.venv/bin/python tools/tgbot.py
```

5. Check `http://127.0.0.1:8090/health` and send `/start` to the bot.
   Set `egress.preferred_country: IN` in the native configuration if you want
   the same pre-download JioSaavn country check as the Docker stack.

## systemd units

`/etc/systemd/system/musicfetcherr-api.service`:

```ini
[Unit]
Description=Musicfetcherr download API (uvicorn, 127.0.0.1:8090)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=musicfetcherr
Group=musicfetcherr
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
User=musicfetcherr
Group=musicfetcherr
WorkingDirectory=/opt/musicfetcherr
ExecStart=/opt/musicfetcherr/.venv/bin/python tools/tgbot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Adjust the example paths and service account for your installation. The account
must be able to read the checkout and write the configured staging and music
directories. Install and enable the units with the following commands.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now musicfetcherr-api musicfetcherr-bot
sudo systemctl status musicfetcherr-api musicfetcherr-bot
```

The bot unit declares `After=musicfetcherr-api.service` so it doesn't race the
API on boot.

## Operations

- Keep `config.yaml` private. It contains the bot token and may contain media
  server credentials.
- Restarting the API clears running jobs and their in-memory results.
- Inspect API logs using `journalctl -u musicfetcherr-api -f`. Use
  `journalctl -u musicfetcherr-bot -f` for Telegram problems.
- The [qBittorrent guide](flac-research.md) covers the optional FLAC tier.
