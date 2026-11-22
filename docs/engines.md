# Music sources, matching, and tagging

Musicfetcherr uses source adapters for searching and downloading. In a native
deployment, `engine.order` controls which adapters participate in search and
their order in results. The supplied Docker stacks use JioSaavn and YouTube Music.
qBittorrent is optional. See [qBittorrent setup](flac-research.md).

| Source | Output | Notes |
| --- | --- | --- |
| JioSaavn | AAC in `.m4a` | Tries the available quality tiers, up to 320 kbps. The bundled stack checks for Indian public egress just before download |
| YouTube Music | AAC in `.m4a` | Uses yt-dlp for metadata and format 140 for downloads. Availability varies by video and player client |
| Original YouTube video | AAC in `.m4a` | Tries the video ID from the playlist when music-source attempts fail |
| qBittorrent | `.flac` | Optional. Requires a separately configured client with writable staging, shared download storage, and access to the relevant files |

## Playlist matching and fallback

For Spotify and YouTube links, albums, and playlists, an enabled JioSaavn source is tried first. The matcher
uses the song title rather than the uploader name. It recognises some upload
credits such as “song from” and Music Mojo credits. When present, song-artist
metadata and a duration within 45 seconds help identify the recording. Titles
are normalised for case and punctuation. Different spellings or transliterations
can still prevent a match. Searches progressively use title with the supplied
artists, primary artist, album when available, and title alone. Candidate
titles lose soundtrack and featured-artist credits before comparison. A small
spelling variation is accepted only with a matching artist and duration within
10 seconds. Live, remix, and other version markers must agree. Search and source
buttons put JioSaavn before YouTube Music. An explicit source selection uses
the chosen source.

If JioSaavn cannot match or download the song, Musicfetcherr tries YouTube
Music. That matcher also considers artist, title, duration, and version markers
such as live or instrumental. If both routes fail, it tries the exact video ID
from the playlist. That video may be a music clip, edit, or cover. Entries
labelled as full movies are skipped. A YouTube watch link with a playlist
parameter generally resolves as a single video. Use the playlist page’s link
to submit the list. A seeded YouTube Mix may return different tracks for
different sessions.

Spotify links provide track, artist, album, and duration information for
matching. Musicfetcherr searches JioSaavn first and uses YouTube Music when
JioSaavn cannot match or deliver the recording. The public Spotify playlist
view exposes up to 100 entries to the app.

Single-track downloads try the selected source first. qBittorrent can also be
tried on a failed download when it is enabled and the track can be identified.
Each attempt has its own status and error, and the successful attempt is used
to label the downloaded source in Telegram. YouTube can require sign-in or
withhold the requested format. Removed videos need another available recording.

For a single YouTube link, Musicfetcherr reads the video metadata directly.
If playback metadata is blocked, it tries YouTube’s public oEmbed title lookup
using the Docker host connection, then searches JioSaavn with that title.
If both lookups fail, the bot asks for a song-name search or a Spotify link.
The single-link picker receives validated JioSaavn alternatives from the resolver.
Uploader names are not used as singer identity when comparing those alternatives.
Each link has its own source buttons, and its original video remains available
as a fallback if the selected source fails.

## Library output

When duplicate checks are enabled, the library is searched by artist and title
before download and checked again with the downloaded metadata before saving.
Different artist tags, titles, or spellings may prevent a duplicate match.

Musicfetcherr saves available tags and cover art and can enrich results using
MusicBrainz, Cover Art Archive, and LRCLIB. It can embed lyrics and write a
synced `.lrc` sidecar when available. Plex and Navidrome rescans are optional
and run after successful library writes.
