"""Export rule issues as CSV for people who work in Excel.

Written as UTF-8 with a BOM (utf-8-sig): without it Excel guesses a legacy code
page and mangles non-ASCII room names such as "Küche".
"""

import csv
from pathlib import Path

CSV_COLUMNS = [
    "rule_id", "severity", "status", "storey", "ifc_class", "element", "element_name",
    "global_id", "message", "rule_title", "reference",
]


def issues_to_csv(issues: list[dict], out_path: str | Path) -> Path:
    path = Path(out_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(issues)
    return path
