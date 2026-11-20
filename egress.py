"""Where is our traffic leaving from right now? (India exit node vs fallback)

The Tailscale watchdog may switch the downloader from the India exit node to
your normal internet connection. JioSaavn only works from India
without a Pro account, so while we're on fallback the API skips JioSaavn and
uses YouTube Music instead.

Config (config.yaml):
    egress:
      preferred_country: IN
      jiosaavn_when_fallback: false   # true if you have JioSaavn Pro
"""
from __future__ import annotations

import logging
import threading
import time
import urllib.request

log = logging.getLogger("egress")

_URLS = ("https://ipinfo.io/country", "https://ifconfig.co/country-iso")


def lookup_country(timeout: float = 8) -> str:
    for url in _URLS:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                c = r.read().decode().strip().upper()
                if len(c) == 2:
                    return c
        except Exception:
            continue
    return ""


class Egress:
    def __init__(self, cfg: dict, max_age: float = 60):
        e = (cfg or {}).get("egress") or {}
        self.preferred = (e.get("preferred_country") or "").upper()
        self.jio_on_fallback = bool(e.get("jiosaavn_when_fallback"))
        self.max_age = max_age
        self._country, self._at = "", 0.0
        self._lock = threading.Lock()

    def country(self) -> str:
        with self._lock:
            if time.monotonic() - self._at > self.max_age:
                c = lookup_country()
                if c and c != self._country:
                    log.info("egress country: %s", c)
                self._country, self._at = c, time.monotonic()
            return self._country

    def on_fallback(self) -> bool:
        """True when we know we're NOT leaving from the preferred country."""
        if not self.preferred:
            return False
        c = self.country()
        return bool(c) and c != self.preferred

    def jiosaavn_ok(self) -> bool:
        return self.jio_on_fallback or not self.on_fallback()
