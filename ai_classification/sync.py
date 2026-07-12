"""Background sync thread — polls the external ticketing system for status updates."""

import json
import logging
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen

from .config import settings

_log = logging.getLogger(__name__)

SYNC_STAMP = Path(__file__).parent.parent / ".last_sync"


# Read the last sync timestamp from file, default to epoch
def _read_last_sync() -> str:
    try:
        return SYNC_STAMP.read_text().strip()
    except FileNotFoundError:
        return "2020-01-01T00:00:00+00:00"


# Save the latest sync timestamp to file
def _write_last_sync(ts: str) -> None:
    SYNC_STAMP.write_text(ts)


# Start a daemon thread that polls the external ticketing system every N seconds
def start_sync_worker(store) -> None:
    api = settings.ticketing_api_url.rstrip("/")
    interval = settings.sync_interval_seconds
    _log.info("Sync worker started — every %ss from %s", interval, api)

    def _run():
        while True:
            try:
                since = _read_last_sync()
                url = f"{api}/incidents/sync?since={since.replace('+', '%2B')}"
                req = Request(url)
                resp = urlopen(req, timeout=10)
                data = json.loads(resp.read().decode())

                updated = data.get("updated", [])
                if updated:
                    latest = since
                    for inc in updated:
                        store.upsert_from_external(
                            inc["id"],
                            inc.get("title", ""),
                            inc.get("description", ""),
                            inc.get("status", "open"),
                            inc.get("created_at", since),
                        )
                        updated_at = inc.get("updated_at", since)
                        if updated_at > latest:
                            latest = updated_at

                    if latest > since:
                        _write_last_sync(latest)

                    _log.info("Synced %d incidents (since %s)", len(updated), since[:19])
                else:
                    _log.debug("Sync — no changes (since %s)", since[:19])

            except Exception as exc:
                _log.error("Sync failed: %s", exc)

            time.sleep(interval)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
