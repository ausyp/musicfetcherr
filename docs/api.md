# REST API

The API listens at `http://127.0.0.1:8090` by default. In the supplied Docker
stack, connect from the Musicfetcherr app container. The Compose files publish
the Tailscale UDP port while the API listens on loopback.

For a native API-only deployment, an optional `api.token` enables the
`X-API-Key` request header. Requests to protected endpoints return `401` if the
key is missing or invalid. `/health` is open. The bundled Telegram client expects
a loopback API with an empty token.

## Endpoints

| Method and path | Request body | Result |
| --- | --- | --- |
| `GET /health` | None | API process health |
| `POST /api/search` | `{"query":"Example Song"}` | Job ID. The result contains `candidates` and `count` |
| `POST /api/download` | `{"query":"Example Song","pick":{…}}` | Job ID. Use the complete candidate from a search result |
| `POST /api/resolve` | `{"url":"…","light":true}` | Job ID. Returns link metadata and a track count for playlists, or track data for single links. Omit `light` for full resolution |
| `POST /api/playlist` | `{"url":"…"}` | Job ID. Downloads the tracks associated with the link |
| `GET /api/job/{job_id}` | None | Status, progress, result, and error for a job. Unknown IDs return `404` |
| `POST /api/job/{job_id}/cancel` | `{}` | Requests cancellation for a playlist job |
| `GET /api/jobs` | None | Jobs held in memory, in submission order |

## Job lifecycle

Submit a job, save the returned `job_id`, and poll its status endpoint. Jobs
run serially. A successful HTTP response means the API accepted the request or
returned a status. Use the job’s `status` field to determine the outcome.

| Status | Meaning |
| --- | --- |
| `queued` | Waiting for the worker |
| `running` | Work has started. A playlist can expose track counts and its current operation |
| `done` | The job completed. A playlist can have both successful and failed tracks |
| `error` | The job itself failed. Check its top-level `error` |
| `cancelled` | The playlist stopped. A running job can return partial results |

Job state and results are in memory. Restarting the API clears them.

## Results and cancellation

A successful download includes the output path, saved format and metadata, and
the `attempts` list. A failed download can include the attempts beside the
top-level error. Playlist results include `outputs`, `errors`, `skipped`
for songs already in the library, and `ignored` for full movies. Failed tracks
include the title, error, search query, and recorded source attempts when
available.

An attempt records the source and a status such as `ok`, `failed`,
`no_match`, or `skipped`. Use the last successful attempt to identify the
delivered source. An original playlist video is reported separately from a
YouTube Music match.

To cancel a playlist, send `{}` if it was submitted without a `chat_id`.
If it was submitted with a `chat_id`, send the same ID in the cancellation body.
Cancelling a queued job returns `cancelled` immediately. Cancelling a running
job returns `cancelling`. Poll for its terminal status after its current
operation returns.

## Example using curl

These commands assume the default loopback API and use `jq` to read the JSON.

```bash
BASE=http://127.0.0.1:8090
AUTH=()
if [ -n "${API_TOKEN:-}" ]; then
  AUTH=(-H "X-API-Key: $API_TOKEN")
fi

JOB_ID=$(curl -fsS "${AUTH[@]}" -H 'Content-Type: application/json' \
  -d '{"query":"Example Artist Example Song"}' "$BASE/api/search" | jq -r .job_id)

curl -fsS "${AUTH[@]}" "$BASE/api/job/$JOB_ID" | jq .
```

Once the search job is `done`, submit a candidate from that job.

```bash
PICK=$(curl -fsS "${AUTH[@]}" "$BASE/api/job/$JOB_ID" | jq -ec '.result.candidates[0]')
jq -n --arg query 'Example Artist Example Song' --argjson pick "$PICK" \
  '{query: $query, pick: $pick}' |
  curl -fsS "${AUTH[@]}" -H 'Content-Type: application/json' -d @- "$BASE/api/download"
```

For an installation walkthrough, see the [setup guide](SETUP.md).
