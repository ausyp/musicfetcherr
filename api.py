"""Musicfetcherr REST API.

  POST /api/search        {query}                    -> {job_id}  (job.result = candidates)
  POST /api/download      {query, pick}              -> {job_id}  (job.result = output)
  POST /api/resolve       {url}                      -> {job_id}  (link -> track pick or playlist track list)
  POST /api/playlist      {url}                      -> {job_id}  (link -> downloads everything)
  GET  /api/job/{id}                                 -> job status
  GET  /api/jobs                                     -> all jobs
  GET  /health

Flow: search job -> caller picks a candidate -> download job with that pick.
Pasted links (YouTube/YouTube Music/Spotify) go through /api/resolve for
inspection, or straight to /api/playlist to download everything. Engines are
jiosaavn (primary, 320kbps AAC) + youtube_music (fallback, ~128kbps AAC) per
config engine.order; a pick's 'source' field routes the download.

Auth: optional — if api.token is set in config.yaml, clients must send
header X-API-Key. Bind is localhost by default.
"""
from __future__ import annotations

import os
import shutil
import sys
import time
import urllib.request
from collections import Counter

import yaml
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engines import EngineError, get_engine, MultiEngine  # noqa: E402
from engines.youtube_music import YoutubeMusicEngine  # noqa: E402
from naming import target_path  # noqa: E402
from postprocess import PostProcessor  # noqa: E402
from jobqueue import Queue  # noqa: E402
from resolvers import (  # noqa: E402
    detect, jiosaavn_youtube_match, match, match_with_rescue, spotify_tracklist, youtube_tracklist,
)
from plex import PlexScanner  # noqa: E402
from navidrome import NavidromeScanner  # noqa: E402
from egress import Egress  # noqa: E402
from library import Library  # noqa: E402

CFG_PATH = os.environ.get("MUSICFETCHERR_CONFIG") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "config.yaml")
with open(CFG_PATH, encoding="utf-8") as fh:
    CFG = yaml.safe_load(fh)

API_TOKEN = CFG.get("api", {}).get("token") or ""

engine = get_engine(CFG)
post = PostProcessor(CFG)
plex = PlexScanner(CFG)
navidrome = NavidromeScanner(CFG)
egress = Egress(CFG)
library = Library(CFG["output"]["root"])
SKIP_EXISTING = (CFG.get("library") or {}).get("skip_existing", True)
# YT Music engine used directly by the URL resolver for art-track matching —
# a pasted Spotify track must land on the clean studio version, not on a
# JioSaavn cover (merged search would let covers in via duration ties).
ytm = YoutubeMusicEngine(CFG)


def _resolve_url(url: str) -> tuple[dict, dict]:
    """detect(url) + full tracklist; raises EngineError on bad input."""
    d = detect(url)
    if not d:
        raise EngineError(f"unsupported link: {url}")
    if d["source"] == "spotify":
        pl = spotify_tracklist(d["kind"], d["id"])
    else:
        pl = youtube_tracklist(url)
    return d, pl


def _qbit_engine():
    engs = engine.engines if isinstance(engine, MultiEngine) else [engine]
    for e in engs:
        if getattr(e, "name", None) == "qbit":
            return e
    return None


def _jio_engine():
    """JioSaavn engine for playlist matching and download fallback."""
    engs = engine.engines if isinstance(engine, MultiEngine) else [engine]
    for e in engs:
        if getattr(e, "name", None) == "jiosaavn":
            return e
    return None


class DownloadFailed(EngineError):
    def __init__(self, reason: str, attempts: list[dict]):
        super().__init__(reason)
        self.attempts = attempts


def _download_with_failover(pick: dict, *, jio_for_youtube=None,
                            prior_attempts: list[dict] | None = None,
                            original_video: dict | None = None) -> tuple:
    """engine.download(pick); on failure automatically falls back to the qbit
    FLAC (torrent) tier. Returns (local_path, attempts_trail) — the trail lets
    the caller report exactly what happened (lossy fail -> FLAC fallback).

    The fallback reuses the failed pick's song title + real duration: qbit
    locates the packed album on the torrent index, then matches the exact song
    file inside it by title AND verifies duration against the reference (±5s).
    """
    attempts = list(prior_attempts or [])
    src = pick.get("source")
    original_selected = (src == "youtube_music" and original_video is not None
                         and pick.get("track_id") == original_video["track_id"])
    source_label = "youtube_original" if original_selected else src
    skipped = False
    try:
        if src == "jiosaavn":
            reason = egress.jiosaavn_skip_reason()
            if reason:
                skipped = True
                raise EngineError(reason)
        local = engine.download(pick)
        attempts.append({"source": source_label, "status": "ok"})
        return local, attempts
    except Exception as exc:
        attempts.append({"source": source_label, "status": "skipped" if skipped else "failed",
                         "error": str(exc)})
        # JioSaavn (or any non-YT source) failed -> same song from YouTube Music.
        if src != "youtube_music" and pick.get("title") and not prior_attempts:
            try:
                yp = match(ytm, {"artist": pick.get("artist") or "",
                                 "title": pick.get("title") or "",
                                 "duration": pick.get("duration") or 0},
                           authoritative_duration=False)
                if yp:
                    local = ytm.download(yp)
                    pick.update(yp)  # tag/meta from the pick we actually got
                    attempts.append({"source": "youtube_music", "status": "ok",
                                     "note": "YouTube Music fallback"})
                    return local, attempts
                attempts.append({"source": "youtube_music", "status": "no_match",
                                 "error": "No matching track found"})
            except Exception as yex:
                attempts.append({"source": "youtube_music", "status": "failed",
                                 "error": str(yex)})
        if src == "youtube_music" and jio_for_youtube is not None and not prior_attempts:
            try:
                jp = jiosaavn_youtube_match(jio_for_youtube, pick)
                if jp is None:
                    attempts.append({"source": "jiosaavn", "status": "no_match"})
                else:
                    reason = egress.jiosaavn_skip_reason()
                    if reason:
                        attempts.append({"source": "jiosaavn", "status": "skipped", "error": reason})
                    else:
                        local = engine.download(jp)
                        pick.update(jp)
                        attempts.append({"source": "jiosaavn", "status": "ok",
                                         "note": "JioSaavn fallback"})
                        return local, attempts
            except Exception as jex:
                attempts.append({"source": "jiosaavn", "status": "failed", "error": str(jex)})
        if original_video is not None and not original_selected:
            try:
                local = engine.download(original_video)
                pick.clear()
                pick.update(original_video)
                attempts.append({"source": "youtube_original", "status": "ok"})
                return local, attempts
            except Exception as video_error:
                attempts.append({"source": "youtube_original", "status": "failed",
                                 "error": str(video_error) or type(video_error).__name__})
        qbit = _qbit_engine()
        # Only attempt the FLAC fallback when we actually have a song identity
        # to search the torrent index with. An empty query (dead-video link,
        # metadata fetch failed -> video_pick degraded to title/artist "") just
        # returns the index's most-seeded albums at random and can never match —
        # skip it rather than burn calls on nonsense (2026-08-30).
        q = f"{pick.get('artist', '')} {pick.get('title', '')}".strip()
        if qbit is not None and src != "qbit" and q:
            try:
                cands = qbit.search(q, limit=8)
                if not cands:
                    raise EngineError(f"no FLAC pack on torrent index for '{q}'")
                best = cands[0]  # search() returns healthiest (most seeders) first
                fp = dict(pick)
                fp.update(
                    source="qbit",
                    track_id=best["track_id"],      # apibay torrent id
                    info_hash=best["info_hash"],    # for the magnet
                    title=pick.get("title"),        # SONG title -> in-pack file match
                    album=best["title"],            # pack/album name
                )
                local = qbit.download(fp)
                attempts.append({"source": "qbit", "status": "ok",
                                 "note": "FLAC fallback", "pack": best["title"]})
                return local, attempts
            except Exception as fex:
                attempts.append({"source": "qbit", "status": "failed",
                                 "error": str(fex)})
        elif src != "qbit":
            attempts.append({"source": "qbit", "status": "skipped",
                             "error": "FLAC fallback is not configured" if qbit is None else
                             "Song title and artist are unavailable"})
        raise DownloadFailed(str(exc) or type(exc).__name__, attempts) from exc


def _download_one(pick: dict, query: str, album_hint: str = "",
                  tracknumber: int | None = None, force_album: str = "",
                  dir_artist: str | None = None, *, jio_for_youtube=None,
                  prior_attempts: list[dict] | None = None,
                  original_video: dict | None = None) -> dict:
    """Download + post-process one pick. Shared by /api/download and the
    /api/playlist loop so both paths tag identically.

    force_album: overrides the track's own album tag (album-link downloads —
    every track belongs to the linked album, and YT Music's per-track page
    tag may point at a single/compilation instead).
    dir_artist: album-artist override for the directory only (album stays
    together under one artist folder while track artist tags stay true).
    """
    if SKIP_EXISTING:
        have = library.find(pick.get("artist"), pick.get("title"))
        if have:
            return _already(pick, query, have)
    local, attempts = _download_with_failover(
        pick, jio_for_youtube=jio_for_youtube, prior_attempts=prior_attempts,
        original_video=original_video)
    meta = engine.meta(local, pick, query)
    if force_album:
        meta["album"] = force_album
    elif album_hint and not meta.get("album"):
        meta["album"] = album_hint
    if tracknumber is not None:
        meta["tracknumber"] = tracknumber
    # YouTube art-track picks sometimes carry no thumbnail — the /vi/ endpoint
    # serves a deterministic JPEG for any video id.
    if not meta.get("cover_url") and pick.get("source") == "youtube_music" and pick.get("track_id"):
        meta["cover_url"] = f"https://i.ytimg.com/vi/{pick['track_id']}/maxresdefault.jpg"

    lyrics = post.lyrics(meta["artist"], meta["title"])
    cover = post.cover(meta.get("release_mbid"), meta["artist"], meta.get("album") or "")
    if not cover and meta.get("cover_url"):
        # engine-provided art (e.g. JioSaavn album art) when MusicBrainz misses;
        # retry with hqdefault when maxres doesn't exist for the video.
        for cu in dict.fromkeys([meta["cover_url"],
                                 meta["cover_url"].replace("maxresdefault", "hqdefault")]):
            try:
                with urllib.request.urlopen(cu, timeout=15) as r:
                    cover = r.read()
                if cover:
                    break
            except Exception:
                cover = None

    dest = target_path(meta, CFG["output"]["root"], local.suffix, dir_artist=dir_artist)
    album_dir = os.path.dirname(dest)
    if SKIP_EXISTING:
        # second check with the real tags: same song already in this album
        # folder (or under this artist) -> keep the existing file
        have = library.find(meta.get("artist"), meta.get("title"), album_dir=album_dir)
        if have:
            try:
                os.remove(local)
            except OSError:
                pass
            return _already(pick, query, have)
    os.makedirs(album_dir, exist_ok=True)
    # staging lives on / (root fs) while the library is on /mnt/hdd — different
    # devices, so os.replace would raise EXDEV. shutil.move copies across mounts.
    shutil.move(str(local), dest)
    post.apply(dest, meta, lyrics, cover)
    post.write_folder_jpg(album_dir, cover)
    library.add(dest)
    plex.request_scan(album_dir)  # Plexamp: refresh just this album folder
    navidrome.request_scan()      # Navidrome: one debounced scan per batch

    return {
        "query": query,
        "picked": pick,
        "output": dest,
        "format": local.suffix,
        "meta": {k: v for k, v in meta.items() if v and k != "unknown_album"},
        "lyrics": bool(lyrics and (lyrics.get("synced") or lyrics.get("plain"))),
        "cover": cover is not None,
        "attempts": attempts,
    }


def _already(pick: dict, query: str, path: str) -> dict:
    return {"query": query, "picked": pick, "output": path, "skipped": True,
            "format": os.path.splitext(path)[1], "meta": {}, "attempts": []}


def run_job(payload: dict) -> dict:
    kind = payload.get("kind")
    if kind == "search":
        cands = engine.search(payload["query"])
        if not cands:
            raise EngineError(f"no results for: {payload['query']}")
        return {"candidates": cands, "count": len(cands)}

    if kind == "resolve":
        d, pl = _resolve_url(payload["url"])
        if d["kind"] == "track" and d["source"] == "youtube":
            pick = ytm.video_pick(d["id"])
            return {"kind": "track", "source": "youtube",
                    "name": pick.get("title") or d["id"], "pick": pick}
        if payload.get("light") and not (d["kind"] == "track" and len(pl["tracks"]) == 1):
            # quick answer for the bot: what is this link? (matching every
            # track here would double the work of the playlist job)
            return {"kind": d["kind"], "source": d["source"], "name": pl["name"],
                    "total": len(pl["tracks"]), "limited": pl.get("limited")}
        for t in pl["tracks"]:
            t["pick"] = match_with_rescue(ytm, t,
                                          authoritative_duration=(d["source"] == "spotify"),
                                          jio=_jio_engine())
        matched = sum(1 for t in pl["tracks"] if t.get("pick"))
        return {"kind": d["kind"], "source": d["source"], "name": pl["name"],
                "tracks": pl["tracks"], "total": len(pl["tracks"]), "matched": matched}

    if kind == "playlist":
        d, pl = _resolve_url(payload["url"])
        if queue.cancellation_requested():
            return {}
        if d["kind"] == "track" and d["source"] == "youtube":
            # a single-link paste -> just download it. If the video's metadata
            # fetch failed (video removed/private/region-blocked), video_pick
            # degraded to an empty title — there's nothing to search any engine
            # with, so fail clearly instead of a doomed download + pointless
            # FLAC attempt that surfaces the raw youtube error (2026-08-30).
            pick = ytm.video_pick(d["id"])
            if queue.cancellation_requested():
                return {}
            if not (pick.get("title") or "").strip():
                raise EngineError(
                    "this YouTube video is unavailable (removed/private/"
                    "region-blocked) and no song title could be fetched — so "
                    "YouTube Music and FLAC fallback searches need a song title. "
                    "Paste a Spotify link or search by song name.")
            return _download_one(pick, pick.get("title") or d["id"])
        # Album links (YT Music OLAK album auto-playlists, Spotify albums)
        # mean every track belongs to ONE album: force the album tag and keep
        # the whole album under its main artist's folder. Plain user playlists
        # keep each track's own album/artist.
        is_album = d["id"].startswith("OLAK5uy_") or (d["source"] == "spotify" and d["kind"] == "album")
        dir_artist = None
        if is_album and pl["tracks"]:
            # Album artist = majority artist across the tracklist (the first
            # track may be a collab — LEO-NiNE opens with the PABLO duet, but
            # the album is LiSA's). The whole album folder hangs under that.
            counts = Counter(t.get("artist") for t in pl["tracks"] if t.get("artist"))
            if counts:
                dir_artist = counts.most_common(1)[0][0]
        outputs, errors, skipped, ignored = [], [], [], []
        jio = _jio_engine()
        jio_for_youtube = jio if d["source"] == "youtube" else None
        n = len(pl["tracks"])

        def progress(i, current=""):
            queue.set_progress({"name": pl["name"], "total": n, "done": i,
                                "downloaded": len(outputs), "skipped": len(skipped),
                                "failed": len(errors), "ignored": len(ignored), "current": current})

        progress(0)
        for i, t in enumerate(pl["tracks"], 1):
            if queue.cancellation_requested():
                break
            artist, title = (t.get("artist") or "").strip(), (t.get("title") or "").strip()
            query = " ".join(filter(None, (artist, title))) or t.get("ref") or ""
            label = " - ".join(filter(None, (artist, title))) or query or f"Track {i}"
            progress(i - 1, label)
            if d["source"] == "youtube" and t.get("full_movie"):
                ignored.append({"track": label, "reason": "Full movie"})
                continue
            track_info = {"track": label, "search_query": query}
            original_video = None
            if d["source"] == "youtube" and t.get("ref"):
                original_video = {
                    "source": "youtube_music", "track_id": t["ref"],
                    "artist": t.get("song_artist") or artist or "Unknown Artist",
                    "title": title or t["ref"], "duration": t.get("duration") or 0,
                }
            if SKIP_EXISTING and not t.get("pick"):
                have = library.find(t.get("artist"), t.get("title"))
                if have:
                    skipped.append({"track": label, "output": have})
                    continue
            attempts = []
            match_error = None
            try:
                pick = t.get("pick") or match_with_rescue(
                    ytm, t, authoritative_duration=(d["source"] == "spotify"), jio=jio)
            except Exception as exc:
                pick = None
                match_error = f"Track matching failed: {exc or type(exc).__name__}"
            if queue.cancellation_requested():
                break
            if not pick and (jio_for_youtube is not None or original_video is not None):
                attempts.append({"source": "youtube_music",
                                 "status": "failed" if match_error else "no_match",
                                 "error": match_error})
            if not pick and jio_for_youtube is not None:
                try:
                    pick = jiosaavn_youtube_match(jio_for_youtube, t)
                    if not pick:
                        attempts.append({"source": "jiosaavn", "status": "no_match"})
                except Exception as exc:
                    attempts.append({"source": "jiosaavn", "status": "failed",
                                     "error": f"Track matching failed: {exc or type(exc).__name__}"})
                if queue.cancellation_requested():
                    break
            if not pick and original_video is not None:
                pick = dict(original_video)
            if not pick:
                error = match_error or ("No matching track found on YouTube Music"
                                        + (" or JioSaavn" if jio else ""))
                errors.append({**track_info, "error": error, "attempts": attempts})
                continue
            try:
                # album links: force the album title; spotify tracks in plain
                # playlists carry their own album; youtube playlists fall back
                # to the playlist title.
                if is_album:
                    hint, force = pl["name"], pl["name"]
                elif d["source"] == "spotify":
                    hint, force = t.get("album") or "", ""
                else:
                    hint, force = pl["name"], ""
                out = _download_one(pick, pick.get("query") or f"{t['artist']} {t['title']}",
                                    album_hint=hint, force_album=force,
                                    tracknumber=t.get("tracknumber") or i,
                                    dir_artist=dir_artist, jio_for_youtube=jio_for_youtube,
                                    prior_attempts=attempts, original_video=original_video)
                if out.get("skipped"):
                    skipped.append({"track": label, "output": out["output"]})
                else:
                    outputs.append(out)
            except DownloadFailed as exc:
                errors.append({**track_info, "error": str(exc), "attempts": exc.attempts})
            except Exception as exc:
                errors.append({**track_info, "error": str(exc) or type(exc).__name__})
        progress(len(outputs) + len(errors) + len(skipped) + len(ignored))
        return {"name": pl["name"], "total": len(pl["tracks"]),
                "downloaded": len(outputs), "errors": errors, "outputs": outputs,
                "skipped": skipped, "ignored": ignored, "limited": pl.get("limited")}

    # download + post-process
    return _download_one(payload["pick"], payload.get("query", ""))


queue = Queue(run_job)
app = FastAPI(title="Musicfetcherr", version="0.1")


def _check_api_key(x_api_key: str | None):
    if API_TOKEN and x_api_key != API_TOKEN:
        raise HTTPException(401, "invalid API key")


class SearchReq(BaseModel):
    query: str


class DownloadReq(BaseModel):
    query: str
    pick: dict


class UrlReq(BaseModel):
    url: str
    light: bool = False  # resolve only: albums/playlists return name + count, no per-track matching


class CancelPlaylistReq(BaseModel):
    chat_id: int | None = None


class PlaylistReq(UrlReq):
    chat_id: int | None = None


@app.get("/health")
def health():
    return {"ok": True, "time": time.time()}


@app.post("/api/search")
def api_search(req: SearchReq, x_api_key: str | None = Header(None)):
    _check_api_key(x_api_key)
    if not req.query.strip():
        raise HTTPException(400, "query required")
    job_id = queue.submit({"kind": "search", "query": req.query.strip()})
    return {"job_id": job_id, "status": "queued"}


@app.post("/api/download")
def api_download(req: DownloadReq, x_api_key: str | None = Header(None)):
    _check_api_key(x_api_key)
    if not req.query.strip() or not req.pick:
        raise HTTPException(400, "query and pick required")
    job_id = queue.submit({"kind": "download", "query": req.query.strip(), "pick": req.pick})
    return {"job_id": job_id, "status": "queued"}


@app.post("/api/resolve")
def api_resolve(req: UrlReq, x_api_key: str | None = Header(None)):
    _check_api_key(x_api_key)
    if not req.url.strip():
        raise HTTPException(400, "url required")
    job_id = queue.submit({"kind": "resolve", "url": req.url.strip(), "light": req.light})
    return {"job_id": job_id, "status": "queued"}


@app.post("/api/playlist")
def api_playlist(req: PlaylistReq, x_api_key: str | None = Header(None)):
    _check_api_key(x_api_key)
    if not req.url.strip():
        raise HTTPException(400, "url required")
    job_id = queue.submit({"kind": "playlist", "url": req.url.strip(), "chat_id": req.chat_id})
    return {"job_id": job_id, "status": "queued"}


@app.post("/api/job/{job_id}/cancel")
def api_cancel_playlist(job_id: str, req: CancelPlaylistReq, x_api_key: str | None = Header(None)):
    _check_api_key(x_api_key)
    status = queue.cancel_playlist(job_id, req.chat_id)
    if status is None:
        raise HTTPException(404, "unknown playlist job")
    return {"job_id": job_id, "status": status}


@app.get("/api/job/{job_id}")
def api_job(job_id: str, x_api_key: str | None = Header(None)):
    _check_api_key(x_api_key)
    job = queue.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    return job


@app.get("/api/jobs")
def api_jobs(x_api_key: str | None = Header(None)):
    _check_api_key(x_api_key)
    return queue.all()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=CFG["api"]["host"], port=CFG["api"]["port"])
