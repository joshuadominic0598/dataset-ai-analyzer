"""Capture stable local files as immutable snapshots."""

import hashlib
import time
from pathlib import Path

from ..data import MAX_BYTES, now

def watch_file(server, source, dataset_id, interval, stop_event=None):
    """Capture stable full-file snapshots, including before a later overwrite."""
    source = Path(source).expanduser().resolve()
    server.store.dataset(dataset_id)
    versions = server.store.versions(dataset_id)
    last_digest = versions[-1]["digest"] if versions else None
    previous_stat = None
    server.watch_status = {
        "file": source.name,
        "dataset": dataset_id,
        "status": "watching",
        "message": "Waiting for a stable file.",
    }
    while True:
        if stop_event is not None:
            if stop_event.wait(interval):
                return
        else:
            time.sleep(interval)
        try:
            if len(server.store.versions(dataset_id)) >= 5:
                server.watch_status.update(
                    status="capacity_reached",
                    message="Five versions are preserved. Start another timeline to capture more.",
                )
                return
            stat = source.stat()
            signature = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
            if signature != previous_stat:
                previous_stat = signature
                continue
            if stat.st_size > MAX_BYTES:
                raise ValueError("The watched file exceeds 20 MB.")
            content = source.read_bytes()
            if source.stat().st_mtime_ns != stat.st_mtime_ns:
                previous_stat = None
                continue
            digest = hashlib.sha256(content).hexdigest()
            if digest == last_digest:
                continue
            with server.jobs_lock:
                active = server.active_jobs.get(dataset_id)
                if active and server.store.run(active)["status"] == "running":
                    continue
                server.store.ingest(dataset_id, "Captured " + now()[:19], content)
            last_digest = digest
            server.watch_status.update(
                status="watching", message="Saved a new snapshot. Generate a report to review it."
            )
        except (ValueError, OSError) as exc:
            server.watch_status.update(status="needs_attention", message=str(exc))
