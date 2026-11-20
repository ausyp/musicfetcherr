#!/usr/bin/env python3
"""Exit-node watchdog: India when it's up, the normal connection when it's not.

Runs next to the Tailscale sidecar (shared network namespace + the tailscaled
socket). Every CHECK_INTERVAL seconds it checks the India exit node:
  * on India:   node answers `tailscale ping` AND traffic really leaves in
                PREFERRED_COUNTRY. FAIL_THRESHOLD bad checks in a row
                -> clear the exit node (fallback: your normal connection).
  * on fallback: node answers RECOVER_THRESHOLD pings in a row
                -> set the exit node again.
With EXIT_NODE_FALLBACK=false it never falls back (downloads just stall until
India returns), but it still re-applies the exit node if it was cleared.

Optional Telegram notice on every switch (uses the bot token + allowed chats).
"""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, "/app")
from egress import lookup_country  # noqa: E402


def env(n, d=""):
    return os.environ.get(n, d).strip()


SOCK = env("TS_SOCKET", "/var/run/tailscale/tailscaled.sock")
EXIT = env("TS_EXIT_NODE")
PREFERRED = (env("PREFERRED_COUNTRY") or env("REQUIRE_COUNTRY") or "IN").upper()
FALLBACK = env("EXIT_NODE_FALLBACK", "true").lower() == "true"
INTERVAL = int(env("FALLBACK_CHECK_INTERVAL", "30"))
FAILS = int(env("FALLBACK_FAIL_THRESHOLD", "3"))
RECOVER = int(env("FALLBACK_RECOVER_THRESHOLD", "2"))
TG_TOKEN = env("TELEGRAM_BOT_TOKEN")
TG_CHATS = [c for c in env("TELEGRAM_ALLOWED_CHATS").replace(" ", "").split(",") if c]
NOTIFY = env("FALLBACK_NOTIFY", "true").lower() == "true"


def log(msg):
    print(f"[watchdog] {msg}", flush=True)


def ts(*args, timeout=30):
    return subprocess.run(["tailscale", f"--socket={SOCK}", *args],
                          capture_output=True, text=True, timeout=timeout)


def using_exit_node() -> bool:
    try:
        st = json.loads(ts("status", "--json").stdout or "{}")
        return bool(st.get("ExitNodeStatus"))
    except Exception:
        return False


def exit_node_alive() -> bool:
    try:
        r = ts("ping", "--until-direct=false", "-c", "3", "--timeout=5s", EXIT, timeout=25)
        return r.returncode == 0
    except Exception:
        return False


def notify(text):
    if not (NOTIFY and TG_TOKEN and TG_CHATS):
        return
    for chat in TG_CHATS:
        try:
            data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
            urllib.request.urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                                   data=data, timeout=15)
        except Exception as exc:
            log(f"telegram notify failed: {exc}")


def use_india():
    r = ts("set", f"--exit-node={EXIT}", "--exit-node-allow-lan-access=true")
    if r.returncode == 0:
        log(f"exit node {EXIT} restored")
        time.sleep(5)
        notify(f"🇮🇳 India exit node is back - JioSaavn downloads re-enabled "
               f"(egress: {lookup_country() or '?'})")
    else:
        log(f"could not set exit node: {r.stderr.strip()}")


def use_fallback():
    r = ts("set", "--exit-node=")
    if r.returncode == 0:
        time.sleep(5)
        c = lookup_country() or "?"
        log(f"exit node {EXIT} down - fell back to direct connection ({c})")
        notify(f"⚠️ India exit node unavailable - downloading via {c} for now. "
               f"JioSaavn is paused; YouTube Music is used instead.")
    else:
        log(f"could not clear exit node: {r.stderr.strip()}")


def main():
    if not EXIT:
        sys.exit("[watchdog] TS_EXIT_NODE is not set")
    log(f"watching exit node {EXIT} (preferred {PREFERRED}, fallback "
        f"{'ON' if FALLBACK else 'OFF'}, every {INTERVAL}s)")
    # wait for tailscaled
    for _ in range(60):
        if ts("status").returncode == 0:
            break
        time.sleep(5)
    bad = good = 0
    while True:
        on_india = using_exit_node()
        if on_india:
            healthy = exit_node_alive() and lookup_country() == PREFERRED
            bad = 0 if healthy else bad + 1
            good = 0
            if bad:
                log(f"exit node check failed ({bad}/{FAILS})")
            if bad >= FAILS and FALLBACK:
                use_fallback()
                bad = 0
        else:
            alive = exit_node_alive()
            good = good + 1 if alive else 0
            bad = 0
            # Fallback OFF: put the exit node back immediately, even if it's
            # down, so nothing ever leaves via the local connection.
            if good >= RECOVER or not FALLBACK:
                use_india()
                good = 0
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
