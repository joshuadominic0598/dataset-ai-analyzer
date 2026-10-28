"""Reproducible injected changes; the manifest is independent of the detector."""

import copy
import csv
import io
import json
from pathlib import Path

from .analysis import build_report

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "house_prices_dataset/train/train.csv"


def csv_bytes(columns, rows):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode()


def fallback_rows():
    """A portable synthetic demo when the supplied private source is absent."""
    rows = []
    neighborhoods = ["North", "South", "East", "West"]
    for i in range(1, 1001):
        rows.append({"Id": str(i), "SalePrice": str(125000 + (i * 7919) % 250000),
                     "GrLivArea": str(850 + (i * 127) % 2200), "Neighborhood": neighborhoods[i % 4],
                     "YearBuilt": str(1950 + i % 65), "LotArea": str(5000 + (i * 233) % 15000),
                     "Alley": "NA" if i % 4 else "Pave"})
    return list(rows[0]), rows


def fixtures(source=SOURCE):
    if source and Path(source).exists():
        with Path(source).open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            columns, original = list(reader.fieldnames), list(reader)
        source_name = "Supplied Ames housing sample; ListingNotes are synthetic."
    else:
        columns, original = fallback_rows()
        source_name = "Fully synthetic housing sample; no source dataset required."
    columns = columns + ["ListingNotes"]
    for row in original:
        row["ListingNotes"] = ("Synthetic example listing. " +
            ("A quiet family home near parks and schools, with room for a garden and a flexible living space." if int(row["Id"]) % 2 else
             "A compact city property close to transit and local shops, suited to convenient everyday living."))
    lookup = {r["Id"]: r for r in original}
    v1 = copy.deepcopy(original[:600])
    v2 = copy.deepcopy(original[:660])
    v3 = [copy.deepcopy(r) for r in v2 if not 601 <= int(r["Id"]) <= 610]
    for row in v3:
        i = int(row["Id"])
        if 1 <= i <= 40:
            row["SalePrice"] = ""
        if 641 <= i <= 652:
            row["SalePrice"] = str(9_000_000 + i * 100)
            row["GrLivArea"] = str(40_000 + i)
        if 631 <= i <= 640:
            row["Neighborhood"] = "Unverified"
    for i in range(50):
        row = copy.deepcopy(original[100 + i])
        row["Id"] = str(2001 + i)
        v3.append(row)
    v3.extend([copy.deepcopy(next(r for r in v3 if r["Id"] == key)) for key in ("500", "501")])
    v4, seen = [], set()
    for row in v3:
        if row["Id"] in seen:
            continue
        seen.add(row["Id"])
        row = copy.deepcopy(row)
        i = int(row["Id"])
        if i <= 660:
            row["SalePrice"] = lookup[row["Id"]]["SalePrice"]
            row["GrLivArea"] = lookup[row["Id"]]["GrLivArea"]
            row["Neighborhood"] = lookup[row["Id"]]["Neighborhood"]
        # Deliberate representation-only migration plus a renamed field.
        row["SaleAmount"] = f"{float(row.pop('SalePrice')):.1f}"
        v4.append(row)
    v4_columns = ["SaleAmount" if c == "SalePrice" else c for c in columns]
    v5 = copy.deepcopy(v4)
    for i, base in enumerate(original[700:1000]):
        row = copy.deepcopy(base)
        row["Id"] = str(3001 + i)
        row["SaleAmount"] = f"{float(row.pop('SalePrice')):.1f}"
        row["SourceBatch"] = "Partner import"
        v5.append(row)
    for row in v5[:len(v4)]:
        row["SourceBatch"] = "Existing portfolio"
    manifest = {
        "source": source_name,
        "versions": [600, 660, 702, 700, 1000],
        "cases": [
            {"pair": [1, 2], "checks": {"movement.added": 60, "movement.removed": 0, "movement.changed": 0, "movement.unchanged": 600, "movement.exact": True}},
            {"pair": [2, 3], "checks": {"movement.added": 50, "movement.removed": 10, "movement.changed": 62, "movement.unchanged": 586,
                                             "movement.exact": False, "movement.ambiguous_identities": 2, "movement.duplicate_identity_rows_after": 4}},
            {"pair": [3, 4], "checks": {"after": 700, "movement.changed": 62, "movement.exact": False}},
            {"pair": [4, 5], "checks": {"movement.added": 300, "movement.removed": 0, "movement.changed": 0, "movement.unchanged": 700, "movement.exact": True}},
        ],
        "expected_signals": [
            {"from": 2, "to": 3, "kind": "missing_values", "column": "SalePrice", "after": 40},
            {"from": 2, "to": 3, "kind": "identity_conflict"},
            {"from": 2, "to": 3, "kind": "unusual_values", "column": "SalePrice"},
            {"from": 2, "to": 3, "kind": "unusual_values", "column": "GrLivArea"},
            {"from": 2, "to": 3, "kind": "value_mix", "column": "Neighborhood"},
            {"from": 3, "to": 4, "kind": "missing_values", "column": "SaleAmount", "after": 0},
            {"from": 3, "to": 4, "kind": "possible_rename", "column": "SaleAmount"},
            {"from": 3, "to": 4, "kind": "number_format", "column": "SaleAmount"},
            {"from": 4, "to": 5, "kind": "record_count", "after": 1000},
            {"from": 4, "to": 5, "kind": "schema"},
        ],
        "notes": ["40 sale prices blanked; 12 records given extreme prices and living areas; 10 neighborhood labels altered.",
                  "V3 includes 50 new IDs, 10 removals, and two repeated identities. Ambiguous record counts are intentionally incomplete.",
                  "V4 repairs those values, removes repeated identities, renames SalePrice to SaleAmount, and writes prices as decimals.",
                  "V5 adds 300 records and SourceBatch. Existing records remain unchanged on shared fields."]}
    snapshots = [("01 · Original", csv_bytes(columns, v1)), ("02 · New listings", csv_bytes(columns, v2)),
                 ("03 · Problematic import", csv_bytes(columns, v3)), ("04 · Repairs and field rename", csv_bytes(v4_columns, v4)),
                 ("05 · Partner expansion", csv_bytes(v4_columns + ["SourceBatch"], v5))]
    return snapshots, manifest


def load_demo(store, dataset_id):
    snapshots, manifest = fixtures()
    for label, content in snapshots:
        store.ingest(dataset_id, label, content)
    store.configure(dataset_id, {"identity": ["Id"], "important": ["SalePrice", "SaleAmount", "GrLivArea"],
        "setup_complete": True, "context": "Housing portfolio. Sudden extreme prices or living areas warrant source verification. Empty prices prevent valuation. ListingNotes are synthetic demonstration text."})
    return manifest


def evaluate(report, manifest):
    checks = []
    for case in manifest["cases"]:
        pair = next(p for p in report["comparisons"] if [p["from"], p["to"]] == case["pair"])
        for path, expected in case["checks"].items():
            actual = pair
            for part in path.split("."):
                actual = actual.get(part) if isinstance(actual, dict) else None
            checks.append({"case": f"V{case['pair'][0]}→V{case['pair'][1]} {path}", "expected": expected, "actual": actual, "passed": actual == expected})
    for signal in manifest["expected_signals"]:
        matches = [f for f in report["findings"] if all(f.get(k) == v for k, v in signal.items() if k != "after")]
        passed = bool(matches) and ("after" not in signal or any(f["metrics"].get("after") == signal["after"] for f in matches))
        checks.append({"case": "signal " + json.dumps(signal), "passed": passed, "matched_evidence": [f["id"] for f in matches]})
    return {"passed": sum(c["passed"] for c in checks), "total": len(checks), "checks": checks,
            "scope": "Exact record counts and expected signal presence. This does not grade AI interpretation or establish the absence of false positives."}
