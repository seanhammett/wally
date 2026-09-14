"""Daily restriction records → days-per-commune counts.

The uncompressed JSON is a single ~540 MB array, so it is parsed by streaming
top-level array elements instead of json.load-ing the lot into memory.
"""
import json
import zipfile
from typing import Iterator

import pandas as pd

from pipeline.common import Log, is_metropolitan, normalise_insee

USAGES = ("AEP", "SOU", "SUP")
SEVERITY = {"vigilance": 1, "alerte": 2, "alerte_renforcee": 3, "crise": 4}
SEVERITY_NAME = {v: k for k, v in SEVERITY.items()}


def stream_elements(fh, chunk_size: int = 1 << 20) -> Iterator[dict]:
    """Yield each object of a top-level JSON array without buffering the whole file."""
    decoder = json.JSONDecoder()
    buf = ""
    started = False
    while True:
        chunk = fh.read(chunk_size)
        if not chunk:
            break
        buf += chunk
        pos = 0
        while True:
            # Skip whitespace, the opening bracket and inter-element commas.
            while pos < len(buf) and buf[pos] in " \t\r\n,":
                pos += 1
            if pos < len(buf) and buf[pos] == "[" and not started:
                started = True
                pos += 1
                continue
            if pos < len(buf) and buf[pos] == "]":
                return
            if pos >= len(buf):
                break
            try:
                obj, end = decoder.raw_decode(buf, pos)
            except ValueError:
                break  # incomplete element; pull another chunk
            yield obj
            pos = end
        buf = buf[pos:]


def transform(ctx) -> None:
    res = ctx.meta["resource"]
    zip_path = ctx.raw_dir / res["filename"]
    if not zip_path.exists():
        raise RuntimeError(f"missing {zip_path}; run fetch first")

    rows = []
    with zipfile.ZipFile(zip_path) as zf, zf.open(res["member"]) as raw:
        fh = __import__("io").TextIOWrapper(raw, encoding="utf-8")
        for i, rec in enumerate(stream_elements(fh), 1):
            code = normalise_insee((rec.get("commune") or {}).get("code"))
            if code is None or not is_metropolitan(code):
                continue
            days_any = 0
            days_severe = 0
            worst = 0
            for day in rec.get("restrictions", []):
                levels = [SEVERITY.get(day.get(u)) for u in USAGES]
                levels = [lv for lv in levels if lv]
                if not levels:
                    continue
                top = max(levels)
                days_any += 1
                if top >= SEVERITY["alerte_renforcee"]:
                    days_severe += 1
                worst = max(worst, top)
            rows.append(
                {
                    "code_insee": code,
                    "jours_restriction": days_severe,
                    "jours_vigilance": days_any,
                    "niveau_max_secheresse": SEVERITY_NAME.get(worst, "aucun"),
                }
            )
            if i % 10000 == 0:
                Log.info(f"  {i:,} communes streamed")

    df = pd.DataFrame(rows).drop_duplicates(subset="code_insee").sort_values("code_insee")
    never = int((df["jours_vigilance"] == 0).sum())
    Log.info(
        f"{len(df):,} communes · median {int(df['jours_restriction'].median())} days at "
        f"alerte renforcée+ · {never:,} ({never / len(df):.1%}) never restricted"
    )
    df.to_csv(ctx.out_path, index=False)
