from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path


def parse_official_sector_payload(payload: str) -> list[dict[str, object]]:
    pattern = re.compile(
        r'"label":"(?P<symbol>[A-Z0-9&-]+)\s+[0-9.]+%","weight":(?P<weight>[0-9.]+).*?"date":"(?P<date>[0-9-]+)"'
    )
    rows = []
    for match in pattern.finditer(payload):
        rows.append(
            {
                "symbol": match.group("symbol"),
                "weight_pct": float(match.group("weight")),
                "source_date": match.group("date"),
            }
        )
    if not rows:
        raise ValueError("official payload did not contain constituent weights")
    total = sum(float(row["weight_pct"]) for row in rows)
    if not 99.5 <= total <= 100.5:
        raise ValueError(
            f"constituent weights total {total:.4f}, expected approximately 100"
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a reviewed Bank Nifty constituent snapshot from a downloaded official NSE Indices payload."
    )
    parser.add_argument(
        "payload",
        type=Path,
        help="Downloaded SectorialIndexDataNIFTY BANK_Sector.js file",
    )
    parser.add_argument(
        "--current-snapshot",
        type=Path,
        required=True,
        help="Existing reviewed snapshot used for names and ownership groups",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    rows = parse_official_sector_payload(args.payload.read_text(encoding="utf-8"))
    current = json.loads(args.current_snapshot.read_text(encoding="utf-8"))
    metadata = {str(row["symbol"]): row for row in current.get("constituents", [])}
    missing = sorted({str(row["symbol"]) for row in rows} - set(metadata))
    removed = sorted(set(metadata) - {str(row["symbol"]) for row in rows})
    if missing or removed:
        raise ValueError(
            f"constituent change requires manual name/group review; added={missing}, removed={removed}"
        )
    source_date = str(rows[0]["source_date"])
    parsed_date = date.fromisoformat("-".join(reversed(source_date.split("-"))))
    output = dict(current)
    output["source_date"] = parsed_date.isoformat()
    output["effective_date"] = parsed_date.isoformat()
    output["strategy_version"] = "banknifty_option_buying_v3"
    output["constituents"] = [
        {
            **{
                key: value
                for key, value in metadata[str(row["symbol"])].items()
                if key != "weight_pct"
            },
            "weight_pct": row["weight_pct"],
        }
        for row in rows
    ]
    args.output.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
