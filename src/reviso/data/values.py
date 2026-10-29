"""Dataset limits, value normalization, and serialized records."""

import json
import math
import re
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

MAX_BYTES = 20 * 1024 * 1024
MAX_ROWS = 100_000
MAX_COLUMNS = 200
MAX_CELL_CHARS = 20_000
ANALYSIS_DEFAULTS = {
    "dataset_type": "unknown",
    "analysis_goal": "",
    "row_meaning": "",
    "target_column": "",
    "date_column": "",
    "date_format": "auto",
    "time_grain": "month",
    "column_roles": {},
    "category_orders": {},
}
WRITE_LOCK = threading.RLock()

def now():
    return datetime.now(timezone.utc).isoformat()

def uid():
    return uuid.uuid4().hex

def dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

def quote(name):
    return '"' + name.replace('"', '""') + '"'

def canonical(value):
    if value is None or not str(value).strip():
        return None
    value = str(value).strip()
    # Codes such as 001 must not collapse into 1.
    if re.match(r"^[+-]?0\d+", value):
        return value
    try:
        number = Decimal(value)
        if number.is_finite():
            return str(number.normalize()) if number else "0"
    except InvalidOperation:
        pass
    return value

def numeric(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None
