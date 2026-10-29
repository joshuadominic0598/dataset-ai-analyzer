import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from reviso.data import Store
from reviso.server import watch_file

class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "reviso.sqlite")
        self.id = self.store.create_dataset("Watched")["id"]
        self.source = self.root / "source.csv"
        self.source.write_bytes(b"id,value\n1,10\n")
        self.stop = threading.Event()
        self.server = SimpleNamespace(
            store=self.store, jobs_lock=threading.Lock(), active_jobs={}, watch_status=None
        )
        self.thread = threading.Thread(
            target=watch_file,
            args=(self.server, self.source, self.id, 0.02, self.stop),
            daemon=True,
        )
        self.thread.start()
        self.addCleanup(self.end)

    def end(self):
        self.stop.set()
        self.thread.join(timeout=2)

    def await_versions(self, count):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            versions = self.store.versions(self.id)
            if len(versions) >= count:
                return versions
            time.sleep(0.02)
        self.fail(f"Watcher did not capture {count} versions: {self.server.watch_status}")

    def test_overwrite_and_append_keep_previous_source_bytes(self):
        first = self.await_versions(1)[0]
        self.source.write_bytes(b"id,value\n1,20\n")
        self.await_versions(2)
        with self.source.open("ab") as handle:
            handle.write(b"2,30\n")
        versions = self.await_versions(3)
        self.assertEqual([v["row_count"] for v in versions], [1, 1, 2])
        self.assertEqual(self.store.source(self.id, first["id"]), b"id,value\n1,10\n")

    def test_unmodified_file_does_not_create_duplicate_versions(self):
        self.await_versions(1)
        time.sleep(0.12)
        self.assertEqual(len(self.store.versions(self.id)), 1)

    def test_invalid_intermediate_file_does_not_consume_version(self):
        self.await_versions(1)
        self.source.write_bytes(b"id,value\n1,20,extra\n")
        time.sleep(0.12)
        self.assertEqual(len(self.store.versions(self.id)), 1)
        self.source.write_bytes(b"id,value\n1,20\n")
        self.assertEqual(len(self.await_versions(2)), 2)

    def test_fifth_version_stops_capture_without_eviction(self):
        self.await_versions(1)
        for i in range(2, 6):
            self.source.write_text(f"id,value\n1,{i}\n")
            self.await_versions(i)
        self.thread.join(timeout=1)
        self.assertEqual(self.server.watch_status["status"], "capacity_reached")
        self.assertEqual(len(self.store.versions(self.id)), 5)

if __name__ == "__main__":
    unittest.main()
