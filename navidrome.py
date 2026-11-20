"""Navidrome library-scan trigger (Subsonic API `startScan`).

After a download lands in the library, Navidrome is asked to rescan so the
song shows up straight away instead of waiting for the scheduled scan.

Scans are debounced: a whole album/playlist download fires ONE scan a few
seconds after the last track lands, not one per track.

Config (config.yaml):
    navidrome:
      url: http://192.168.1.20:4533
      user: admin
      password: ""
      debounce_seconds: 15
Leave url empty to disable.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import threading

import requests

log = logging.getLogger("navidrome")


class NavidromeScanner:
    def __init__(self, cfg: dict):
        n = (cfg or {}).get("navidrome") or {}
        self.url = (n.get("url") or "").rstrip("/")
        self.user = n.get("user") or ""
        self.password = n.get("password") or ""
        self.delay = float(n.get("debounce_seconds") or 15)
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.user and self.password)

    def _auth(self) -> dict:
        salt = secrets.token_hex(6)
        token = hashlib.md5((self.password + salt).encode()).hexdigest()
        return {"u": self.user, "t": token, "s": salt,
                "v": "1.16.1", "c": "musicfetcherr", "f": "json"}

    def scan_now(self) -> bool:
        if not self.enabled:
            return False
        try:
            r = requests.get(f"{self.url}/rest/startScan", params=self._auth(), timeout=15)
            body = r.json().get("subsonic-response", {})
            ok = body.get("status") == "ok"
            if ok:
                log.info("navidrome scan started")
            else:
                log.warning("navidrome scan refused: %s", body.get("error"))
            return ok
        except Exception as exc:  # never break a download over a scan
            log.warning("navidrome scan failed: %s", exc)
            return False

    def request_scan(self) -> None:
        """Debounced: (re)start the timer; the scan fires once things go quiet."""
        if not self.enabled:
            return
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.delay, self.scan_now)
            self._timer.daemon = True
            self._timer.start()
