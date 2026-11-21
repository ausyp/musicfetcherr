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

import urllib.request

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
    def __init__(self, cfg: dict):
        settings = (cfg or {}).get("egress") or {}
        self.preferred = (settings.get("preferred_country") or "").strip().upper()
        self.jio_on_fallback = bool(settings.get("jiosaavn_when_fallback"))

    def jiosaavn_skip_reason(self) -> str | None:
        """Check the current route immediately before a JioSaavn download."""
        if not self.preferred or self.jio_on_fallback:
            return None
        country = lookup_country()
        if country == self.preferred:
            return None
        if not country:
            return f"Could not verify the download connection is in {self.preferred}"
        return f"Download connection is in {country}. JioSaavn requires {self.preferred}"
