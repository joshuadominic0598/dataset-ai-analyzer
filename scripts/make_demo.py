"""Write five reproducible CSVs and the independent expected-change manifest."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reviso.demo import SOURCE, fixtures

parser = argparse.ArgumentParser()
parser.add_argument("--source", type=Path, default=SOURCE)
parser.add_argument("--output", type=Path, default=Path("examples/house_prices"))
args = parser.parse_args()
snapshots, manifest = fixtures(args.source)
args.output.mkdir(parents=True, exist_ok=True)
for i, (_, content) in enumerate(snapshots, 1):
    (args.output / f"housing_v{i}.csv").write_bytes(content)
(args.output / "expected_changes.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
print(f"Created five snapshots and expected_changes.json in {args.output}")
