"""CSV validation and full-file field profiles."""

import csv
import io
import re
from collections import Counter
from decimal import Decimal

from .values import MAX_BYTES, MAX_CELL_CHARS, MAX_COLUMNS, MAX_ROWS, canonical, numeric

def parse_csv(content):
    if len(content) > MAX_BYTES:
        raise ValueError("Each snapshot must be at most 20 MB.")
    try:
        decoded = content.decode("utf-8-sig")
        if any(ord(c) < 32 and c not in "\t\n\r" for c in decoded):
            raise ValueError("CSV files must contain text without binary control characters.")
        reader = csv.reader(io.StringIO(decoded), strict=True)
        header = next(reader)
        if not header or len(header) > MAX_COLUMNS:
            raise ValueError("Use between 1 and 200 columns.")
        header = [v.strip() for v in header]
        if any(not v or len(v) > 120 or any(ord(c) < 32 for c in v) for v in header):
            raise ValueError(
                "Column names must be nonempty, printable, and at most 120 characters."
            )
        if len({v.casefold() for v in header}) != len(header):
            raise ValueError("Repeated column names are ambiguous. Rename them before importing.")
        rows = []
        for line, values in enumerate(reader, 2):
            if not values:
                continue
            if len(values) != len(header):
                raise ValueError(
                    f"CSV record {line} has {len(values)} cells; expected {len(header)}."
                )
            if any(len(v) > MAX_CELL_CHARS for v in values):
                raise ValueError(f"CSV record {line} has a cell longer than 20,000 characters.")
            rows.append(dict(zip(header, [v if v.strip() else None for v in values])))
            if len(rows) > MAX_ROWS:
                raise ValueError("This local edition supports up to 100,000 records per snapshot.")
        return header, rows
    except (UnicodeDecodeError, csv.Error, StopIteration) as exc:
        raise ValueError("Use a valid UTF-8 CSV with a header.") from exc

def profile(header, rows):
    output = {}
    for col in header:
        values = [r[col] for r in rows if r[col] is not None]
        numbers = [numeric(v) for v in values]
        valid_numbers = [v for v in numbers if v is not None]
        leading_codes = any(re.match(r"^[+-]?0\d+", v.strip()) for v in values)
        if values and len(valid_numbers) == len(values) and not leading_codes:
            kind = (
                "integer"
                if all(
                    Decimal(v) == Decimal(v).to_integral_value() and -(2**63) <= Decimal(v) < 2**63
                    for v in values
                )
                else "number"
            )
        elif values and len(valid_numbers) / len(values) >= 0.8 and not leading_codes:
            kind = "mixed"
        else:
            kind = "text" if values else "empty"
        counts = Counter(canonical(v) for v in values)
        mean_len = sum(len(v) for v in values) / max(len(values), 1)
        text_candidate = (
            kind == "text"
            and mean_len >= 50
            and sum(" " in v for v in values) / max(len(values), 1) > 0.5
        )
        ordered = sorted(valid_numbers)
        output[col] = {
            "kind": kind,
            "missing": len(rows) - len(values),
            "missing_pct": round(100 * (len(rows) - len(values)) / max(len(rows), 1), 3),
            "unique_values": len(counts),
            "numeric_values": len(valid_numbers),
            "mean": sum(v / len(valid_numbers) for v in valid_numbers) if valid_numbers else None,
            "min": min(valid_numbers) if valid_numbers else None,
            "max": max(valid_numbers) if valid_numbers else None,
            "q1": ordered[int((len(ordered) - 1) * 0.25)] if ordered else None,
            "q3": ordered[int((len(ordered) - 1) * 0.75)] if ordered else None,
            "top_values": counts.most_common(6),
            "semantic_candidate": text_candidate,
            "average_length": round(mean_len, 1),
        }
    return output
