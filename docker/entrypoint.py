#!/usr/bin/env python3
"""Container entrypoint.

1. Builds config.yaml from environment variables (so the whole thing is
   configured from Portainer's stack environment variables).
2. Starts the watchdog, the API and the Telegram bot, and supervises them.
"""
import os
import signal
import subprocess
import sys
import time

import yaml

CFG_PATH = os.environ.get("MUSICFETCHERR_CONFIG", "/tmp/musicfetcherr/config.yaml")


def env(name, default=""):
    return os.environ.get(name, default).strip()


def preferred_country() -> str:
    return (env("PREFERRED_COUNTRY") or env("REQUIRE_COUNTRY") or "IN").upper()


def build_config() -> dict:
    staging = env("STAGING_DIR", "/data/staging")
    order = [e.strip() for e in env("ENGINE_ORDER", "jiosaavn,youtube_music").split(",") if e.strip()]
    chats = [int(c) for c in env("TELEGRAM_ALLOWED_CHATS").replace(" ", "").split(",") if c]
    return {
        "jiosaavn": {"staging_dir": staging},
        "youtube_music": {"staging_dir": staging},
        "qbit": {
            "url": env("QBIT_URL", "http://127.0.0.1:8080"),
            "username": env("QBIT_USER", "admin"),
            "password": env("QBIT_PASSWORD"),
            "category": "musicfetcherr",
            "staging_dir": staging,
        },
        "engine": {"order": order},
        "output": {"root": env("MUSIC_DIR", "/music"), "unknown_album": "Unknown Album"},
        "postprocess": {
            "musicbrainz": env("POSTPROCESS_MUSICBRAINZ", "true").lower() == "true",
            "lyrics": env("POSTPROCESS_LYRICS", "true").lower() == "true",
            "cover": True,
            "musicbrainz_ua": "Musicfetcherr-Navidrome/0.1 ( self-hosted )",
        },
        "api": {"host": "127.0.0.1", "port": int(env("API_PORT", "8090")), "token": env("API_TOKEN")},
        "telegram_bot": {"token": env("TELEGRAM_BOT_TOKEN"), "allowed_chats": chats},
        "library": {"skip_existing": env("SKIP_EXISTING", "true").lower() == "true"},
        "egress": {
            "preferred_country": preferred_country(),
            "jiosaavn_when_fallback": env("JIOSAAVN_WHEN_FALLBACK", "false").lower() == "true",
        },
        "navidrome": {
            "url": env("NAVIDROME_URL"),
            "user": env("NAVIDROME_USER"),
            "password": env("NAVIDROME_PASSWORD"),
            "debounce_seconds": int(env("NAVIDROME_SCAN_DELAY", "15")),
        },
        "plex": {
            "url": env("PLEX_URL", "http://host.docker.internal:32400"),
            "token": env("PLEX_TOKEN"),
            "section_id": env("PLEX_SECTION_ID"),
            "music_path": env("PLEX_MUSIC_PATH"),
            "debounce_seconds": int(env("PLEX_SCAN_DELAY", "15")),
        },
    }


def write_config(uid: int, gid: int) -> None:
    os.makedirs(os.path.dirname(CFG_PATH), exist_ok=True)
    with open(CFG_PATH, "w", encoding="utf-8") as fh:
        yaml.safe_dump(build_config(), fh, sort_keys=False)
    os.chmod(CFG_PATH, 0o600)
    if os.geteuid() == 0:
        os.chown(CFG_PATH, uid, gid)
        os.chown(os.path.dirname(CFG_PATH), uid, gid)
    print(f"[entrypoint] wrote {CFG_PATH} from environment", flush=True)


def check_music_dir(uid: int, gid: int) -> None:
    """Create the music folder if needed and make sure PUID can write to it,
    so a permissions problem shows up at startup, not on the first download."""
    music = env("MUSIC_DIR", "/music").rstrip("/") or "/music"
    try:
        os.makedirs(music, exist_ok=True)
        if os.geteuid() == 0:
            try:
                os.chown(music, uid, gid)
            except OSError:
                pass  # SMB/CIFS mounts often refuse chown; the write test decides
        probe = os.path.join(music, ".musicfetcherr-write-test")
        kw = {"user": uid, "group": gid, "extra_groups": []} if os.geteuid() == 0 else {}
        ok = subprocess.run(["sh", "-c", f'touch "{probe}" && rm -f "{probe}"'], **kw).returncode == 0
    except Exception as exc:
        ok = False
        print(f"[entrypoint] {exc}", flush=True)
    if ok:
        print(f"[entrypoint] music folder {music} is writable", flush=True)
    else:
        print(f"[entrypoint] WARNING: user {uid}:{gid} cannot write to {music}. Downloads will "
              f"fail. For an SMB volume, mount it with uid={uid},gid={gid} (or file_mode=0777,"
              f"dir_mode=0777); for a host folder, chown it to {uid}:{gid}.", flush=True)


def update_ytdlp() -> None:
    """YouTube changes often and old yt-dlp versions break (403s), so update it
    on every start. Failure is harmless: the version in the image is used."""
    if env("YTDLP_AUTO_UPDATE", "true").lower() != "true":
        return
    try:
        r = subprocess.run(["pip", "install", "-q", "-U", "--disable-pip-version-check",
                            "--root-user-action=ignore", "yt-dlp[default]"],
                           capture_output=True, text=True, timeout=180)
        v = subprocess.run(["python", "-c", "import yt_dlp;print(yt_dlp.version.__version__)"],
                           capture_output=True, text=True, timeout=30).stdout.strip()
        if r.returncode == 0:
            print(f"[entrypoint] yt-dlp up to date ({v})", flush=True)
        else:
            print(f"[entrypoint] yt-dlp update failed, using {v}: {r.stderr.strip()[-200:]}", flush=True)
    except Exception as exc:
        print(f"[entrypoint] yt-dlp update skipped: {exc}", flush=True)


def main():
    """Runs everything in one container:
      watchdog (root, needs the tailscaled socket) -> manages exit-node routing,
      api + bot (as PUID:PGID so music files are owned by you).
    If any process dies, everything stops and Docker restarts the container."""
    uid, gid = int(env("PUID", "1000")), int(env("PGID", "1000"))
    staging = env("STAGING_DIR", "/data/staging")
    os.makedirs(staging, exist_ok=True)
    if os.geteuid() == 0:
        for root, dirs, files in os.walk("/data"):
            for n in [root] + [os.path.join(root, f) for f in files]:
                try:
                    os.chown(n, uid, gid)
                except OSError:
                    pass
    write_config(uid, gid)
    check_music_dir(uid, gid)
    update_ytdlp()

    procs = {}
    as_user = {"user": uid, "group": gid, "extra_groups": []} if os.geteuid() == 0 else {}

    if env("TS_EXIT_NODE"):
        procs["watchdog"] = subprocess.Popen(["python", "/app/docker/watchdog.py"])

    procs["api"] = subprocess.Popen(
        ["uvicorn", "api:app", "--host", "127.0.0.1", "--port", env("API_PORT", "8090")],
        cwd="/app", **as_user)
    time.sleep(3)
    procs["bot"] = subprocess.Popen(["python", "tools/tgbot.py"], cwd="/app", **as_user)

    def stop(*_):
        for p in procs.values():
            if p.poll() is None:
                p.terminate()
        for p in procs.values():
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()

    signal.signal(signal.SIGTERM, lambda *a: (stop(), sys.exit(0)))
    while True:
        for name, p in procs.items():
            if p.poll() is not None:
                print(f"[entrypoint] {name} exited ({p.returncode}) - restarting container", flush=True)
                stop()
                sys.exit(1)
        time.sleep(2)


if __name__ == "__main__":
    main()
