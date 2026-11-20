"""What's already in the music library?

Used to
  * skip songs you already have (same title by the same artist, any album
    folder, any format), so re-downloading a playlist doesn't make duplicates;
  * reuse an existing artist/album folder whose name differs only in
    punctuation or soundtrack noise ("Interstellar" vs "Interstellar (Original
    Motion Picture Soundtrack)"), so one film doesn't end up in two folders.

The library is indexed from folder and file names only (no tag reading), so
it stays fast on network shares. The index is cached and refreshed every
few minutes; files we save are added to it straight away.
"""
from __future__ import annotations

import os
import re
import threading
import time

AUDIO_EXT = {".m4a", ".mp3", ".flac", ".opus", ".ogg", ".aac", ".wav", ".alac", ".wma"}

# Bracketed noise that doesn't change which song/album it is. "Version",
# "Reprise", "Male"/"Female" etc. are deliberately NOT here: those are
# different recordings.
_NOISE = re.compile(
    r"\s*[\(\[][^)\]]*\b(?:from|soundtrack|ost|original motion picture|"
    r"feat\.?|ft\.?|featuring|remaster(?:ed)?)\b[^)\]]*[\)\]]",
    re.I)
_COPY_SUFFIX = re.compile(r"\s\(\d+\)$")  # "Song (2)" from older duplicate saves
_ARTIST_SPLIT = re.compile(r"\s*(?:;|,|&|/|\bx\b|\band\b|\bfeat\.?|\bft\.?|\bfeaturing\b)\s*", re.I)


def norm(s: str | None) -> str:
    """Comparison key: noise removed, lowercase, letters/digits only (any script)."""
    s = _NOISE.sub("", s or "")
    s = re.sub(r"[^\w]+", " ", s.lower(), flags=re.UNICODE).replace("_", " ")
    return " ".join(s.split())


def artist_keys(s: str | None) -> set[str]:
    return {k for k in (norm(p) for p in _ARTIST_SPLIT.split(s or "")) if k}


def match_dir(parent: str, name: str) -> str:
    """Existing child folder of `parent` that is the same as `name` once
    normalised; otherwise `name` unchanged."""
    want = norm(name)
    if not want:
        return name
    try:
        with os.scandir(parent) as it:
            for e in it:
                if e.is_dir() and e.name != name and norm(e.name) == want:
                    return e.name
    except OSError:
        pass
    return name


class Library:
    def __init__(self, root: str, ttl: float = 600):
        self.root = root.rstrip("/")
        self.ttl = ttl
        self._index: dict[str, list[tuple[set[str], str]]] = {}
        self._built: float | None = None
        self._lock = threading.Lock()

    def _scan(self) -> dict:
        idx: dict[str, list[tuple[set[str], str]]] = {}
        for dirpath, dirs, files in os.walk(self.root):
            rel = os.path.relpath(dirpath, self.root)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            if depth >= 3:
                dirs[:] = []
            artist = rel.split(os.sep)[0] if depth else ""
            for f in files:
                stem, ext = os.path.splitext(f)
                if ext.lower() not in AUDIO_EXT:
                    continue
                key = norm(_COPY_SUFFIX.sub("", stem))
                if key:
                    idx.setdefault(key, []).append((artist_keys(artist), os.path.join(dirpath, f)))
        return idx

    def _fresh(self):
        with self._lock:
            if self._built is None or time.monotonic() - self._built > self.ttl:
                self._index = self._scan()
                self._built = time.monotonic()

    def find(self, artist: str | None, title: str | None, album_dir: str | None = None) -> str | None:
        """Path of an existing copy, or None.

        Match = same normalised title AND (an artist in common with the
        artist folder, OR it sits in `album_dir` — the folder this song
        would be saved into).
        """
        key = norm(title)
        if not key:
            return None
        self._fresh()
        want = artist_keys(artist)
        album_dir = os.path.normpath(album_dir) if album_dir else None
        for keys, path in self._index.get(key, []):
            if not os.path.exists(path):
                continue
            if want & keys or (album_dir and os.path.dirname(path) == album_dir):
                return path
        return None

    def add(self, path: str) -> None:
        stem = os.path.splitext(os.path.basename(path))[0]
        rel = os.path.relpath(path, self.root)
        artist = rel.split(os.sep)[0] if os.sep in rel else ""
        with self._lock:
            self._index.setdefault(norm(_COPY_SUFFIX.sub("", stem)), []).append(
                (artist_keys(artist), path))
