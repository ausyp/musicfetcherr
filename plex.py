"""Plex library refresh after downloads (so songs appear in Plexamp).

Debounced like the Navidrome scanner: a whole album/playlist triggers one
refresh per album folder a few seconds after the last track lands.

Config (config.yaml):
    plex:
      url: http://host.docker.internal:32400
      token: ""              # X-Plex-Token
      section_id: ""         # music library id; blank = every music library
      music_path: ""         # the music folder AS PLEX SEES IT (e.g. /data/music).
                             # Set it to refresh only the new album folders
                             # (partial scan); blank = refresh the whole library.
      debounce_seconds: 15
Leave url or token empty to disable.
"""
from __future__ import annotations

import logging
import os
import threading

import requests

log = logging.getLogger("plex")


class PlexScanner:
    def __init__(self, cfg: dict):
        p = (cfg or {}).get("plex") or {}
        self.url = (p.get("url") or "").rstrip("/")
        self.token = p.get("token") or ""
        self.section_id = str(p.get("section_id") or "").strip()
        self.plex_root = (p.get("music_path") or "").rstrip("/")
        self.local_root = ((cfg or {}).get("output") or {}).get("root", "").rstrip("/")
        self.delay = float(p.get("debounce_seconds") or 15)
        self._dirs: set[str] = set()
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.token)

    def _get(self, endpoint: str, **params):
        params["X-Plex-Token"] = self.token
        return requests.get(f"{self.url}{endpoint}", params=params,
                            headers={"Accept": "application/json"}, timeout=15)

    def _sections(self) -> list[str]:
        if self.section_id:
            return [self.section_id]
        r = self._get("/library/sections")
        r.raise_for_status()
        dirs = (r.json().get("MediaContainer") or {}).get("Directory") or []
        return [str(d["key"]) for d in dirs if d.get("type") == "artist"]

    def _to_plex_path(self, local_dir: str) -> str | None:
        if not (self.plex_root and self.local_root and local_dir.startswith(self.local_root)):
            return None
        return self.plex_root + local_dir[len(self.local_root):]

    def refresh_now(self, dirs: set[str] | None = None) -> bool:
        if not self.enabled:
            return False
        try:
            sections = self._sections()
            if not sections:
                log.warning("plex: no music library found")
                return False
            paths = [p for p in (self._to_plex_path(d) for d in (dirs or ())) if p]
            for sid in sections:
                if paths:
                    for p in paths:
                        self._get(f"/library/sections/{sid}/refresh", path=p).raise_for_status()
                else:
                    self._get(f"/library/sections/{sid}/refresh").raise_for_status()
            log.info("plex refresh started (sections %s, %s)", ",".join(sections),
                     f"{len(paths)} folder(s)" if paths else "full library")
            return True
        except Exception as exc:  # never break a download over a scan
            log.warning("plex refresh failed: %s", exc)
            return False

    def _fire(self):
        with self._lock:
            dirs, self._dirs = self._dirs, set()
            self._timer = None
        self.refresh_now(dirs)

    def request_scan(self, album_dir: str | None = None) -> None:
        if not self.enabled:
            return
        with self._lock:
            if album_dir:
                self._dirs.add(os.path.normpath(album_dir))
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.delay, self._fire)
            self._timer.daemon = True
            self._timer.start()
