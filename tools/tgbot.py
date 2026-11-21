#!/usr/bin/env python3
"""Musicfetcherr Telegram bot — thin bridge between Telegram DMs and the
Musicfetcherr API on 127.0.0.1:8090.

Stdlib-only transport (urllib long-poll on getUpdates). One thread per
incoming message so a long-running download poll never blocks replies.
Owner allowlist from config.yaml -> telegram_bot.allowed_chats.

Run: .venv/bin/python tools/tgbot.py   (same venv as the API)
"""
from __future__ import annotations

import html
import json
import os
import logging
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tgbot")

BASE = Path(__file__).resolve().parent.parent
CFG_PATH = Path(os.environ.get("MUSICFETCHERR_CONFIG") or (BASE / "config.yaml"))
CFG = yaml.safe_load(CFG_PATH.read_text())
API = f"http://{CFG['api']['host']}:{CFG['api']['port']}"
TOKEN = (CFG.get("telegram_bot") or {}).get("token") or ""
ALLOWED = set((CFG.get("telegram_bot") or {}).get("allowed_chats") or [])
if not TOKEN:
    raise SystemExit("telegram_bot.token missing in config.yaml")

TG = f"https://api.telegram.org/bot{TOKEN}"
_LINK_RE = re.compile(
    r"(https?://(?:[a-z0-9.-]*\.)?(?:youtube\.com|youtu\.be|music\.youtube\.com|"
    r"open\.spotify\.com|spotify\.link)/\S+|spotify:(?:track|album|playlist):\S+)"
)
_ERR_RE = re.compile(r"^(?:error|failed):?\s*(.*)$", re.I)

_picks: dict[int, dict[int, dict]] = {}  # chat_id -> {n: pick}
_alts: dict[int, dict[int, dict[str, dict]]] = {}  # chat_id -> {n: {source: candidate}}


def tg(method: str, **params) -> dict:
    """Call the Telegram Bot API. Files go as multipart when 'files' given."""
    files = params.pop("files", None)
    timeout = params.pop("_timeout", 60)
    url = f"{TG}/{method}"
    if files:
        boundary = uuid.uuid4().hex
        body = b""
        for k, v in params.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode("utf-8")
        for k, (fn, data) in files.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fn}\"\r\n"
                     f"Content-Type: application/octet-stream\r\n\r\n").encode("utf-8")
            body += data
            body += b"\r\n"
        body += f"--{boundary}--\r\n".encode("utf-8")
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(url, data=json.dumps(params).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def api(method: str, body: dict | None = None, timeout: int = 60) -> dict:
    url = f"{API}{method}"
    if body is not None:
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    else:
        req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def say(chat: int, text: str, **kw):
    try:
        r = tg("sendMessage", chat_id=chat, text=text, parse_mode="HTML", **kw)
        return (r.get("result") or {}).get("message_id")
    except Exception as exc:
        log.warning("sendMessage failed: %s", exc)
        return None


def edit(chat: int, message_id, text: str, **kw) -> bool:
    if not message_id:
        return False
    try:
        tg("editMessageText", chat_id=chat, message_id=message_id, text=text, parse_mode="HTML", **kw)
        return True
    except Exception as exc:  # "message is not modified" etc. — harmless
        log.debug("editMessageText: %s", exc)
        return False


def send_audio(chat: int, path: str, caption: str):
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        say(chat, f"⚠️ file missing: {path} ({exc})")
        return
    try:
        tg("sendAudio", chat_id=chat, caption=caption[:1024],
           files={"audio": (Path(path).name, data)}, _timeout=300)
    except Exception as exc:
        say(chat, f"⚠️ The file was saved to your library. Telegram upload failed ({failure_reason(exc)}).")


def poll_job(job_id: str, cap: int | None = 320, on_progress=None) -> dict:
    """Block until the API job finishes (3s cadence). cap = max polls, or None
    to wait as long as the job is alive (gives up only if the API stops
    answering for ~5 minutes). on_progress(progress_dict) on every change."""
    polls = fails = 0
    last = None
    while cap is None or polls < cap:
        polls += 1
        try:
            j = api(f"/api/job/{job_id}")
            fails = 0
        except Exception:
            fails += 1
            if fails > 100:
                break
            time.sleep(3)
            continue
        if j.get("status") in ("done", "error", "cancelled"):
            return j
        prog = j.get("progress")
        if on_progress and prog and prog != last:
            last = prog
            try:
                on_progress(prog)
            except Exception:
                pass
        time.sleep(3)
    return {"status": "timeout", "error": "the bot stopped waiting; the download may still finish", "result": None}


def fmt_progress(p: dict) -> str:
    name = html.escape(str(p.get("name") or "playlist"))
    total, done = int(p.get("total") or 0), int(p.get("done") or 0)
    bar_n = 12
    filled = round(bar_n * done / total) if total else 0
    bar = "▓" * filled + "░" * (bar_n - filled)
    line = f"⏬ <b>{name}</b>\n{bar} {done}/{total}"
    bits = [f"✅ {p.get('downloaded', 0)}"]
    if p.get("skipped"):
        bits.append(f"⏭ {p['skipped']} already had")
    if p.get("failed"):
        bits.append(f"⚠️ {p['failed']} failed")
    if p.get("ignored"):
        bits.append(f"⏭ {p['ignored']} full movies skipped")
    line += "\n" + " · ".join(bits)
    if p.get("current") and done < total:
        line += f"\n<i>now: {html.escape(str(p['current']))[:80]}</i>"
    return line


LIMIT_NOTE = ("⚠️ Spotify only shares the first {n} songs of a playlist, so only "
              "{n} will be downloaded. For the rest, split the playlist into parts "
              "of {n} or fewer, or send a YouTube Music copy of it (no limit).")


def fmt_dur(s) -> str:
    try:
        s = int(s or 0)
        return f"{s // 60}:{s % 60:02d}"
    except (TypeError, ValueError):
        return "?:??"


_BADGES = {"jiosaavn": "[JS 320]", "youtube_music": "[YT 128]", "qbit": "[FLAC]"}


def fmt_pick(p: dict) -> str:
    src = p.get("source")
    badge = _BADGES.get(src, f"[{html.escape(str(src or '?'))}]")
    title = html.escape(p.get("title") or "?")
    artist = html.escape(p.get("artist") or "?")
    album = html.escape(p.get("album") or "")
    dur = fmt_dur(p.get("duration")) if p.get("duration") else "?:??"
    out = f"{badge} <b>{artist}</b> — {title} ({dur})"
    if album:
        out += f" · {album}"
    if src == "qbit":
        if p.get("seeders") is not None:
            out += f" · 🌱{p['seeders']}"
        if p.get("size_mb"):
            out += f" · {p['size_mb']}MB"
    return out


def _btn(p: dict, n: int) -> str:
    """Short button label for a candidate."""
    if p.get("source") == "qbit":
        s = f"{n}. [FLAC] {p.get('title') or '?'}"
        if p.get("seeders") is not None:
            s += f" · {p['seeders']}🌱"
        return s[:64]
    return f"{n}. {(p.get('artist') or '?')} — {p.get('title') or '?'}"[:64]


def _cand_key(c: dict) -> tuple[str, str]:
    """Exact key — used only to dedupe the displayed result list."""
    return ((c.get("artist") or "").lower().strip(), (c.get("title") or "").lower().strip())


_JUNK = {
    "feat", "feats", "ft", "with", "official", "audio", "video", "lyrics", "lyric",
    "hd", "hq", "flac", "mp3", "aac", "m4a", "opus", "wav", "320", "160", "128",
    "24bit", "16bit", "khz", "bit", "cd", "rip", "web", "webrip", "hdtv", "vinyl",
    "single", "album", "ep", "remaster", "remastered", "deluxe", "edition",
    "explicit", "clean", "the", "a", "an", "and", "of", "to",
}


_WORDS = re.compile(r"[^\W_]+", re.UNICODE)  # word chars in ANY script (CJK, Devanagari…)


def _tokens(s) -> set[str]:
    """Lowercased word tokens, brackets + release/codec noise removed.

    Unicode-aware: a Latin-only `[a-z0-9]+` tokenizer returns an EMPTY set for
    Japanese/Hindi/Korean titles, which silently disabled grouping for every
    non-Latin track (two sources of the same song could never pair).
    """
    s = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", str(s or "").lower())
    return {w for w in _WORDS.findall(s)
            if (len(w) > 1 or not w.isascii()) and w not in _JUNK}


def _raw_tokens(s) -> set[str]:
    """Tokens from the RAW string, brackets kept — that's where variant markers
    live ("Get Lucky (Radio Edit)", "… (Daft Punk Remix)")."""
    return {w for w in _WORDS.findall(str(s or "").lower())
            if len(w) > 1 or not w.isascii()}


_VARIANTS = {
    "remix", "rmx", "live", "acoustic", "instrumental", "karaoke", "cover", "radio",
    "edit", "extended", "mix", "mixed", "demo", "reprise", "sped", "slowed",
    "nightcore", "orchestral", "piano", "reverb", "mashup", "bootleg", "refix",
    "flip", "8d",
}


def _artist_ok(a: dict, b: dict) -> bool:
    """Artist agreement, tolerating torrent rows (which carry no artist field):
    when one side is artistless, look for the other's artist tokens inside its
    release name — that's what keeps a cover by someone else from grouping."""
    aa, ab = _tokens(a.get("artist")), _tokens(b.get("artist"))
    if aa and ab:
        return bool(aa & ab)
    if aa:
        return bool(aa & _tokens(b.get("title")))
    if ab:
        return bool(ab & _tokens(a.get("title")))
    return True


def _same_track(a: dict, b: dict) -> bool:
    """Loose same-track test: strong title-token overlap, IDENTICAL variant
    markers (a remix/live/cover/radio-edit must never pair with the original —
    storefront picks are used verbatim, so a wrong pair would silently file the
    wrong recording), and artist agreement."""
    ta, tb = _tokens(a.get("title")), _tokens(b.get("title"))
    if not ta or not tb:
        return False
    if (_raw_tokens(a.get("title")) & _VARIANTS) != (_raw_tokens(b.get("title")) & _VARIANTS):
        return False
    inter = ta & tb
    ok = len(inter) >= 2 and len(inter) / min(len(ta), len(tb)) >= 0.5
    if not ok and len(inter) == 1 and min(len(ta), len(tb)) == 1:
        ok = True  # one-word title ("Happy") vs a long release name — that token is all we have
    if not ok:
        return False
    return _artist_ok(a, b)


def _cluster(cands: list[dict]) -> tuple[list[dict[str, dict]], dict[int, int]]:
    """Group candidates into "same track, different source" clusters.

    Returns the clusters plus an id(candidate) -> cluster index map. Shared by the
    search flow and the single-track-link flow so both offer identical options.
    """
    groups: list[dict[str, dict]] = []
    gid: dict[int, int] = {}
    for c in cands:
        if not c.get("source"):
            continue
        for i, g in enumerate(groups):
            if _same_track(c, next(iter(g.values()))):
                g.setdefault(str(c["source"]), c)
                gid[id(c)] = i
                break
        else:
            gid[id(c)] = len(groups)
            groups.append({str(c["source"]): c})
    return groups, gid


def _qbtn(c: dict) -> str:
    """Picker button label: quality badge (+ seeders for torrents)."""
    src = c.get("source")
    label = _BADGES.get(src, f"[{html.escape(str(src or '?'))}]")
    if src == "qbit" and c.get("seeders") is not None:
        label += f" {c['seeders']}🌱"
    return label


def probe(path) -> str:
    """Honest, compact quality summary of the delivered file (ffprobe)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries",
             "format=format_name,bit_rate:stream=codec_name,sample_rate,bits_per_raw_sample",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=25)
        j = json.loads(r.stdout or "{}")
    except Exception:
        return ""
    fmt = (j.get("format") or {}).get("format_name", "")
    st = (j.get("streams") or [{}])[0]
    codec = st.get("codec_name") or ""
    sr = st.get("sample_rate")
    bits = st.get("bits_per_raw_sample")
    br = (j.get("format") or {}).get("bit_rate") or st.get("bit_rate")
    try:
        br_k = f"{int(br) // 1000}k" if br else ""
    except (TypeError, ValueError):
        br_k = ""
    if "flac" in fmt:
        q = "FLAC"
        if sr:
            q += f" {float(sr) / 1000:.1f}kHz"
        if bits:
            q += f"/{bits}bit"
        return q
    if codec:
        return codec.upper() + (f" {br_k}" if br_k else "")
    return br_k or fmt or ""


def failure_reason(error, limit: int = 240) -> str:
    reason = " ".join(str(error or "Unknown error").split())
    # Media and proxy URLs can include credentials or signed query strings.
    reason = re.sub(r"https?://[^\s<>]+", "[URL]", reason, flags=re.IGNORECASE)
    return html.escape(reason if len(reason) <= limit else reason[:limit - 1] + "…")


def fmt_attempts(attempts, limit: int = 240) -> str:
    names = {
        "jiosaavn": "JioSaavn", "youtube_music": "YouTube Music",
        "youtube_original": "Original YouTube video", "qbit": "FLAC (qBittorrent)",
    }
    lines = []
    for attempt in attempts or []:
        source = str(attempt.get("source") or "Unknown source")
        label = html.escape(names.get(source, source))
        status = attempt.get("status")
        if status == "ok":
            pack = attempt.get("pack")
            note = f" ({html.escape(str(pack)[:120])})" if pack else ""
            lines.append(f"• {label} — succeeded{note}")
        elif status == "skipped":
            lines.append(f"• {label} — skipped ({failure_reason(attempt.get('error'), limit)})")
        elif status == "no_match":
            lines.append(f"• {label} — no matching track found")
        else:
            lines.append(f"• {label} — failed ({failure_reason(attempt.get('error'), limit)})")
    return "\n".join(lines)


def playlist_failure_messages(errors: list[dict], job_id: str):
    header = f"<b>Failed tracks</b> · Job <code>{html.escape(job_id)}</code>"
    lines = [header]
    size = len(header)
    for i, error in enumerate(errors):
        track = str(error.get("track") or "Unknown track")
        query = str(error.get("search_query", error.get("track")) or "").strip() or track
        query_params = urllib.parse.urlencode({"search_query": query[:240]})
        url = f"https://www.youtube.com/results?{query_params}"
        entry = f'❌ <b>{html.escape(track[:120])}</b> · <a href="{url}">Search YouTube</a>'
        if i < 5:
            detail = fmt_attempts(error.get("attempts"), limit=160) or failure_reason(error.get("error"))
            entry += f"\n{detail}"
        if len(lines) > 1 and size + 2 + len(entry) > 3800:
            yield "\n\n".join(lines)
            lines = [header]
            size = len(header)
        lines.append(entry)
        size += 2 + len(entry)
    if len(lines) > 1:
        yield "\n\n".join(lines)


def job_failure(job: dict, job_id: str, heading: str) -> str:
    attempts = fmt_attempts(job.get("attempts"))
    detail = attempts or failure_reason(job.get("error"))
    return f"{heading}\n{detail}\nJob <code>{html.escape(job_id)}</code>"


# -- flows ----------------------------------------------------------------

def flow_search(chat: int, query: str):
    say(chat, f"🔎 searching <i>{html.escape(query)}</i>…")
    try:
        jid = api("/api/search", {"query": query})["job_id"]
    except Exception as exc:
        say(chat, f"❌ Musicfetcherr API request failed. {failure_reason(exc)}")
        return
    j = poll_job(jid)
    if j["status"] != "done":
        say(chat, job_failure(j, jid, "❌ Search did not complete"))
        return
    if not (j.get("result") or {}).get("candidates"):
        say(chat, f"❌ No results for <i>{html.escape(query)}</i>")
        return
    cands = j["result"]["candidates"]
    # Cluster candidates into "same track, different source" groups. Matching is
    # deliberately tolerant: storefront rows are clean ("Get Lucky (feat. X)") while
    # torrent rows are release names ("Daft Punk feat. X - Get Lucky (Single) FLAC
    # 24-96") — an exact key never pairs them. A loose match is safe because qbit
    # re-verifies the track inside the pack by title+duration at download time, so
    # the worst case is a clean "no match in pack" error, never a wrong file.
    groups, gid = _cluster(cands)
    seen, n = set(), 0
    lines = ["<b>Results</b> — <i>tap a button to download</i> (or reply <code>download N</code>):"]
    store, alts, rows = {}, {}, []
    for c in cands[:15]:
        key = _cand_key(c)
        if key in seen:
            continue
        seen.add(key)
        n += 1
        store[n] = c
        alts[n] = groups[gid[id(c)]] if id(c) in gid else {str(c.get("source") or "?"): c}
        lines.append(f"{n}. {fmt_pick(c)}")
        rows.append([{"text": _btn(c, n), "callback_data": f"dl:{n}"}])
        if n >= 12:
            break
    _picks[chat] = store
    _alts[chat] = alts
    say(chat, "\n".join(lines), reply_markup=json.dumps({"inline_keyboard": rows}))


def flow_download(chat: int, n: int):
    """A tapped result: offer the qualities it's actually available in, else download."""
    pick = (_picks.get(chat) or {}).get(n)
    if not pick:
        say(chat, "❌ no search results in this chat — search first")
        return
    opts = list(((_alts.get(chat) or {}).get(n) or {}).values())
    if len(opts) < 2:  # single source — nothing to choose, don't add a tap
        _do_download(chat, pick)
        return
    art = str(pick.get("artist") or "").strip()
    tit = str(pick.get("title") or "?").strip()
    head = f"{html.escape(art)} — {html.escape(tit)}" if art and art != "?" else html.escape(tit)
    dur = f" ({fmt_dur(pick.get('duration'))})" if pick.get("duration") else ""
    rows = [[{"text": _qbtn(c), "callback_data": f"q:{n}:{c.get('source')}"} for c in opts]]
    say(chat, f"🎚 <b>Choose quality</b> — {head}{dur}",
        reply_markup=json.dumps({"inline_keyboard": rows}))


def _do_download(chat: int, pick: dict):
    q = f"{pick.get('artist', '')} {pick.get('title', '')}".strip()
    say(chat, f"⬇️ downloading {fmt_pick(pick)}…")
    try:
        jid = api("/api/download", {"query": q, "pick": pick})["job_id"]
    except Exception as exc:
        say(chat, f"❌ Musicfetcherr API request failed. {failure_reason(exc)}")
        return
    j = poll_job(jid)
    res = j.get("result") or {}
    tr = fmt_attempts(res.get("attempts"))
    if j["status"] == "done" and res.get("skipped"):
        say(chat, f"⏭ already in your library: {fmt_pick(pick)}\n<code>{html.escape(res['output'])}</code>")
        return
    if j["status"] == "done" and res.get("output"):
        out = res["output"]
        qual = probe(out)
        msg = f"✅ {fmt_pick(pick)}"
        if qual:
            msg += f"\n🎧 <b>{html.escape(qual)}</b>"
        extra = []
        if "lyrics" in res:
            extra.append(f"lyrics {'yes' if res['lyrics'] else 'no'}")
        if "cover" in res:
            extra.append(f"cover {'yes' if res['cover'] else 'no'}")
        if extra:
            msg += f" · ({', '.join(extra)})"
        if len(res.get("attempts") or []) > 1:
            msg += f"\n<b>Sources tried</b>\n{tr}"
        msg += f"\n<code>{html.escape(out)}</code>"
        say(chat, msg)
        try:
            if Path(out).stat().st_size < 50 * 1024 * 1024:
                send_audio(chat, out, f"{pick.get('artist')} — {pick.get('title')}")
        except OSError:
            pass
    else:
        say(chat, job_failure(j, jid, f"❌ Download did not complete for {fmt_pick(pick)}"))


def flow_link(chat: int, url: str):
    """A pasted link. A single track asks which quality; albums/playlists just download."""
    say(chat, f"🔗 resolving <i>{html.escape(url)}</i> — queued…")
    jid = None
    playlist_name = ""
    try:
        jid = api("/api/resolve", {"url": url, "light": True})["job_id"]
    except Exception:
        jid = None
    if jid:
        j = poll_job(jid, cap=60)
        r = j.get("result") or {}
        if j.get("status") == "done" and r.get("kind") == "track" and r.get("pick"):
            offer_link_quality(chat, url, r["pick"])
            return
        if j.get("status") == "done" and r.get("total"):
            kind = {"album": "album", "playlist": "playlist"}.get(r.get("kind"), "list")
            playlist_name = str(r.get("name") or "")
            msg = (f"📋 <b>{html.escape(str(r.get('name') or kind))}</b> — {r['total']} tracks "
                   f"({kind}). Starting download…")
            if r.get("limited"):
                msg += "\n" + LIMIT_NOTE.format(n=r["limited"])
            say(chat, msg)
        elif j.get("status") == "error":
            say(chat, job_failure(j, jid, "❌ Couldn’t read that link"))
            return
    _link_download(chat, url, playlist_name=playlist_name)


def offer_link_quality(chat: int, url: str, pick: dict):
    """Offer the sources that actually hold this track. The pasted link's own
    pick is always on the list, so tapping it is the old download-the-link path."""
    art = str(pick.get("artist") or "").strip()
    tit = str(pick.get("title") or "").strip()
    q = f"{art} {tit}".strip()
    opts: dict[str, dict] = {}
    if q:
        say(chat, "🔎 checking which sources have this track…")
        try:
            jid = api("/api/search", {"query": q})["job_id"]
            j = poll_job(jid, cap=120)
            cands = (j.get("result") or {}).get("candidates") or []
        except Exception:
            cands = []
        groups, _ = _cluster(cands)
        hit = next((g for g in groups if any(_same_track(c, pick) for c in g.values())), None)
        opts = dict(hit or {})
    opts[str(pick.get("source") or "link")] = pick  # the link's own source wins
    if len(opts) < 2:  # nothing to choose — download the link as before
        _link_download(chat, url, cancellable=False)
        return
    _alts[chat] = {**_alts.get(chat, {}), 0: opts}  # slot 0 = "this pasted link"
    head = f"{html.escape(art)} — {html.escape(tit)}" if art else html.escape(tit or "track")
    dur = f" ({fmt_dur(pick.get('duration'))})" if pick.get("duration") else ""
    rows = [[{"text": _qbtn(c), "callback_data": f"q:0:{s}"} for s, c in opts.items()]]
    say(chat, f"🎚 <b>Choose quality</b> — {head}{dur}",
        reply_markup=json.dumps({"inline_keyboard": rows}))


def cancel_playlist(chat: int, job_id: str):
    try:
        status = api(f"/api/job/{job_id}/cancel", {"chat_id": chat}, timeout=10)["status"]
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            say(chat, "This playlist is no longer available to cancel.")
        else:
            say(chat, f"❌ Couldn’t cancel the playlist. {failure_reason(exc)}")
        return
    except Exception as exc:
        say(chat, f"❌ Couldn’t cancel the playlist. {failure_reason(exc)}")
        return
    if status == "cancelling":
        say(chat, "Stopping the playlist after the current track step finishes.")
    elif status != "cancelled":
        say(chat, "This playlist has already finished.")


def _link_download(chat: int, url: str, *, playlist_name: str = "", cancellable: bool = True):
    try:
        jid = api("/api/playlist", {"url": url, "chat_id": chat})["job_id"]
    except Exception as exc:
        say(chat, f"❌ Musicfetcherr API request failed. {failure_reason(exc)}")
        return
    controls = {}
    if cancellable:
        controls["reply_markup"] = {"inline_keyboard": [[
            {"text": "Cancel playlist", "callback_data": f"cancel:{jid}"}
        ]]}
    state = {"mid": None, "at": time.monotonic()}
    if cancellable:
        label = html.escape((playlist_name or url)[:240])
        state["mid"] = say(chat, f"⏳ <b>{label}</b> queued or starting · Job <code>{html.escape(jid)}</code>",
                           link_preview_options={"is_disabled": True}, **controls)

    def on_progress(p):
        text = fmt_progress(p)
        if state["mid"] is None:
            state["mid"] = say(chat, text, **controls)
            state["at"] = time.monotonic()
        elif time.monotonic() - state["at"] >= 10:  # Telegram rate limits edits
            edit(chat, state["mid"], text, **controls)
            state["at"] = time.monotonic()

    j = poll_job(jid, cap=None, on_progress=on_progress)
    clear_controls = {"reply_markup": {"inline_keyboard": []}}
    if j["status"] == "cancelled":
        p = j.get("progress") or {}
        heading = "⏹ <b>Playlist cancelled</b>"
        if p.get("total"):
            heading += (f" after processing {p['done']}/{p['total']} tracks."
                        f" {p['downloaded']} saved to your library.")
        if not edit(chat, state["mid"], heading, **clear_controls):
            say(chat, heading)
        for message in playlist_failure_messages((j.get("result") or {}).get("errors") or [], jid):
            say(chat, message, link_preview_options={"is_disabled": True})
        return
    res = j.get("result") or {}
    if j["status"] != "done":
        heading = "❌ Link job stopped" if j["status"] == "error" else "⚠️ Link job status unavailable"
        p = j.get("progress") or {}
        if p.get("total"):
            heading += (f" after processing {p['done']}/{p['total']} tracks"
                        f" ({p['downloaded']} downloaded)")
        if j["status"] == "error":
            edit(chat, state["mid"], heading, **clear_controls)
        say(chat, job_failure(j, jid, heading))
        return
    if res.get("skipped") is True:
        edit(chat, state["mid"], "⏭ Already in your library", **clear_controls)
        picked = res.get("picked") or {}
        say(chat, f"⏭ already in your library: <b>{html.escape(str(picked.get('artist') or ''))} — "
                  f"{html.escape(str(picked.get('title') or ''))}</b>\n<code>{html.escape(res['output'])}</code>")
        return
    # single-track result shape: {query, picked, output, format, meta, lyrics, cover}
    if res.get("output") and "outputs" not in res:
        picked = res.get("picked") or {}
        art = html.escape(str(picked.get("artist") or ""))
        tit = html.escape(str(picked.get("title") or Path(res["output"]).stem))
        out = res["output"]
        extra = []
        if "lyrics" in res:
            extra.append(f"lyrics {'yes' if res['lyrics'] else 'no'}")
        if "cover" in res:
            extra.append(f"cover {'yes' if res['cover'] else 'no'}")
        msg = f"✅ <b>{art} — {tit}</b>" + (f" ({', '.join(extra)})" if extra else "")
        qual = probe(out)
        tra = fmt_attempts(res.get("attempts"))
        if qual:
            msg += f" · 🎧 <b>{html.escape(qual)}</b>"
        if len(res.get("attempts") or []) > 1:
            msg += f"\n<b>Sources tried</b>\n{tra}"
        msg += f"\n<code>{html.escape(out)}</code>"
        edit(chat, state["mid"], "✅ Download finished", **clear_controls)
        say(chat, msg)
        try:
            if Path(out).stat().st_size < 50 * 1024 * 1024:
                send_audio(chat, out, f"{art} — {tit}")
        except OSError:
            pass
        return
    # album/playlist result shape: {name, total, downloaded, errors, outputs}
    errs = res.get("errors") or []
    name = html.escape(str(res.get("name") or "playlist"))
    total, dl = res.get("total", 0), res.get("downloaded", 0)
    skipped = res.get("skipped") or []
    ignored = res.get("ignored") or []
    icon = "⚠️" if errs else "✅"
    head = f"{icon} <b>{name}</b> — {dl} downloaded"
    if skipped:
        head += f", ⏭ {len(skipped)} already had"
    head += f" (of {total})"
    if ignored:
        count = len(ignored)
        movies = "full movie" if count == 1 else "full movies"
        head += f"\n⏭ Skipped {count} {movies}."
    if res.get("limited"):
        head += f"\n⚠️ Only the first {res['limited']} songs of this Spotify playlist were available."
    if state["mid"]:
        edit(chat, state["mid"], fmt_progress({"name": res.get("name"), "total": total, "done": total,
                                                "downloaded": dl, "skipped": len(skipped),
                                                "failed": len(errs), "ignored": len(ignored)}), **clear_controls)
    outs = res.get("outputs") or []
    if errs:
        head += f"\n⚠️ {len(errs)} tracks failed. The playlist finished processing."
    say(chat, head)
    for message in playlist_failure_messages(errs, jid):
        say(chat, message, link_preview_options={"is_disabled": True})
    recovered = [out for out in outs if len(out.get("attempts") or []) > 1]
    if recovered:
        lines = [f"↪️ <b>{len(recovered)} tracks downloaded using a fallback</b>"]
        for out in recovered[:3]:
            picked = out.get("picked") or {}
            track = html.escape(str(picked.get("title") or "Unknown track")[:120])
            lines.append(f"<b>{track}</b>\n{fmt_attempts(out['attempts'], limit=120)}")
        say(chat, "\n\n".join(lines))
    if len(outs) == 1 and outs[0].get("output"):
        p = outs[0]["output"]
        try:
            if Path(p).stat().st_size < 50 * 1024 * 1024:
                send_audio(chat, p, (res.get("name") or Path(p).stem))
        except OSError:
            pass


def flow_status(chat: int):
    try:
        jobs = api("/api/jobs")
    except Exception as exc:
        say(chat, f"❌ Musicfetcherr API request failed. {failure_reason(exc)}")
        return
    if not jobs:
        say(chat, "🫥 queue is empty")
        return
    by = {}
    for j in jobs:
        by[j.get("status", "?")] = by.get(j.get("status", "?"), 0) + 1
    cur = next((j for j in jobs if j.get("status") == "running"), None)
    msg = "📊 queue: " + " · ".join(f"{k}={v}" for k, v in sorted(by.items()))
    if cur:
        pl = cur.get("payload") or {}
        msg += f"\n▶️ running: {pl.get('kind')} {pl.get('url') or pl.get('query') or ''}"
    say(chat, html.escape(msg))


HELP = (
    "<b>Musicfetcherr</b> 🎵\n\n"
    "• Paste a YouTube / YouTube Music / Spotify link — a single track asks which "
    "quality first; an album/playlist downloads everything.\n"
    "• <code>search &lt;query&gt;</code> — find tracks\n"
    "• <code>download N</code> — grab result N from the last search\n"
    "• tapping a result shows its available qualities (FLAC / 320 / 128) — pick one\n"
    "• Tap <b>Cancel playlist</b> on its queue or progress message to stop it\n"
    "• <code>/status</code> — queue state\n"
    "• <code>/help</code> — this\n\n"
    "Files land in the Jellyfin library; single tracks are also sent here as audio."
)


def handle(chat: int, text: str):
    t = (text or "").strip()
    low = t.lower()
    if low in ("/start", "/help", "help", "start"):
        say(chat, HELP)
        return
    if low == "/status" or low == "status":
        flow_status(chat)
        return
    m = re.match(r"^(?:download|dl)\s+(\d+)$", low)
    if m:
        flow_download(chat, int(m.group(1)))
        return
    m = re.match(r"^search\s+(.+)$", t, re.S)
    if m:
        flow_search(chat, m.group(1).strip())
        return
    m = _LINK_RE.search(t)
    if m:
        flow_link(chat, m.group(0).rstrip(".,)"))
        return
    say(chat, HELP)


def main():
    me = tg("getMe")
    log.info("bot online: @%s (%s)", me["result"]["username"], me["result"]["id"])
    offset = 0
    while True:
        try:
            upd = tg("getUpdates", offset=offset, timeout=25,
                     allowed_updates=["message", "callback_query"])
        except Exception as exc:
            log.warning("getUpdates error: %s", exc)
            time.sleep(3)
            continue
        for u in upd.get("result", []):
            offset = max(offset, u["update_id"] + 1)
            if "callback_query" in u:
                cq = u["callback_query"]
                chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id")
                cid = cq.get("id")
                if cid:
                    try:
                        tg("answerCallbackQuery", callback_query_id=cid)
                    except Exception:
                        pass
                if chat_id is None:
                    continue
                if chat_id not in ALLOWED:
                    say(chat_id, "❌ Not authorized.")
                    continue
                m = re.fullmatch(r"cancel:([a-f0-9]{12})", cq.get("data") or "")
                if m:
                    threading.Thread(target=cancel_playlist, args=(chat_id, m.group(1)),
                                     daemon=True).start()
                    continue
                m = re.match(r"^dl:(\d+)$", cq.get("data") or "")
                if m:
                    threading.Thread(target=flow_download,
                                      args=(chat_id, int(m.group(1))),
                                      daemon=True).start()
                    continue
                m = re.match(r"^q:(\d+):([a-z_]+)$", cq.get("data") or "")
                if m:
                    cand = (((_alts.get(chat_id) or {}).get(int(m.group(1))) or {})
                            .get(m.group(2)))
                    if not cand:
                        say(chat_id, "❌ that choice expired — search again")
                        continue
                    threading.Thread(target=_do_download, args=(chat_id, cand),
                                     daemon=True).start()
                continue
            msg = u.get("message") or {}
            chat_id = (msg.get("chat") or {}).get("id")
            text = msg.get("text") or ""
            if chat_id is None:
                continue
            if chat_id not in ALLOWED:
                log.warning("ignoring unauthorized chat %s: %r", chat_id, text[:80])
                say(chat_id, "❌ Not authorized.")
                continue
            threading.Thread(target=handle, args=(chat_id, text), daemon=True).start()


if __name__ == "__main__":
    main()
