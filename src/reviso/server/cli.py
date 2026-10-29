"""Application command-line entrypoint."""

import argparse
import os
import threading

from ..agent import Trace
from ..data import Store
from .http import AppServer
from .watcher import watch_file

def main():
    parser = argparse.ArgumentParser(description="Analytic Brain — understand your data.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default=os.environ.get("REVISO_DB", ".reviso/reviso.sqlite"))
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Create a five-version house-price demo if no dataset exists.",
    )
    parser.add_argument(
        "--watch", help="Watch a local CSV for stable changes and preserve full snapshots."
    )
    parser.add_argument("--dataset", help="Dataset ID receiving watched snapshots.")
    parser.add_argument("--interval", type=float, default=5)
    args = parser.parse_args()
    store = Store(args.db)
    store.recover_interrupted_runs()
    if args.demo and not store.datasets():
        from ..demo import load_demo

        dataset = store.create_dataset("Ames housing")
        load_demo(store, dataset["id"])
        trace = Trace(store, dataset["id"], "report", "offline", None)
        from ..agent import generate_report

        trace.finish(generate_report(store, dataset["id"], trace))
    server = AppServer(("127.0.0.1", args.port), store)
    if args.watch:
        if not args.dataset or args.interval < 1:
            parser.error("--watch requires --dataset and an --interval of at least one second.")
        store.dataset(args.dataset)
        threading.Thread(
            target=watch_file, args=(server, args.watch, args.dataset, args.interval), daemon=True
        ).start()
    print(f"Analytic Brain is running at http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
