#!/usr/bin/env python3
"""
import_hc_recent.py — Import recent High Court judgments from S3 into Neon.

Courts: Delhi, Bombay, Madras, Calcutta, Allahabad
Years:  2020-2026
Sample: 500 cases per court per year (prioritising description > 300 chars)

After each court's insert, ts_vector is updated in batches of 200.

Usage:
    DATABASE_URL="postgresql://..." python3 import_hc_recent.py
"""

import os
import sys
import re
import time
import urllib.request
import urllib.error

import pandas as pd
import psycopg2

# ── Config ────────────────────────────────────────────────────────────────────

BASE = "https://indian-high-court-judgments.s3.amazonaws.com/"

COURTS = {
    "Delhi HC":      "7_26",
    "Bombay HC":     "27_1",
    "Madras HC":     "33_10",
    "Calcutta HC":   "19_16",
    "Allahabad HC":  "9_13",
}

YEARS = [2020, 2021, 2022, 2023, 2024, 2025, 2026]
SAMPLE_PER_YEAR = 500
TSVEC_BATCH = 200

CASE_TYPE_KEYWORDS = {
    "criminal":      ["cr.", "criminal", "bail", "fir", "murder", "theft", "ipc", "crpc"],
    "civil":         ["civil", "suit", "decree", "injunction", "property", "contract"],
    "constitutional":["writ", "article 226", "article 32", "mandamus", "certiorari", "habeas corpus"],
    "family":        ["matrimonial", "divorce", "custody", "maintenance", "adoption"],
    "labour":        ["labour", "labor", "workman", "industrial", "termination", "retrenchment"],
}

PARQUET_COLS = ["title", "description", "judge", "disposal_nature", "court", "decision_date", "cnr"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def classify_case_type(title: str, description: str) -> str:
    text = (title + " " + description).lower()
    scores = {
        ct: sum(1 for kw in kws if kw in text)
        for ct, kws in CASE_TYPE_KEYWORDS.items()
    }
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "general"


def list_benches(year: int, court_code: str) -> list[str]:
    url = (
        f"{BASE}?list-type=2"
        f"&prefix=metadata/parquet/year={year}/court={court_code}/"
        f"&delimiter=/"
    )
    try:
        xml = urllib.request.urlopen(url, timeout=15).read().decode()
        return re.findall(r"bench=([^/]+)/", xml)
    except Exception:
        return []


def fetch_parquet(year: int, court_code: str, bench: str) -> pd.DataFrame:
    url = (
        f"{BASE}metadata/parquet/year={year}/court={court_code}"
        f"/bench={bench}/metadata.parquet"
    )
    tmp = f"/tmp/hc_{court_code}_{year}_{bench}.parquet"
    urllib.request.urlretrieve(url, tmp)
    df = pd.read_parquet(tmp, columns=PARQUET_COLS)
    os.remove(tmp)
    return df


def sample_cases(df: pd.DataFrame) -> pd.DataFrame:
    """Return up to SAMPLE_PER_YEAR rows, prioritising description length > 300."""
    df = df.copy()
    df["description"] = df["description"].fillna("").astype(str)
    df["title"] = df["title"].fillna("").astype(str)

    # Drop completely empty rows
    df = df[df["description"].str.strip().astype(bool)]
    df = df[df["title"].str.strip().astype(bool)]

    if len(df) <= SAMPLE_PER_YEAR:
        return df

    long_desc = df[df["description"].str.len() > 300]
    short_desc = df[df["description"].str.len() <= 300]

    if len(long_desc) >= SAMPLE_PER_YEAR:
        return long_desc.sample(n=SAMPLE_PER_YEAR, random_state=42)

    # Fill remainder from short descriptions
    remaining = SAMPLE_PER_YEAR - len(long_desc)
    filler = short_desc.sample(n=min(remaining, len(short_desc)), random_state=42)
    return pd.concat([long_desc, filler], ignore_index=True)


# ── DB operations ─────────────────────────────────────────────────────────────

def insert_cases(conn, df: pd.DataFrame) -> int:
    cur = conn.cursor()
    inserted = 0

    for _, row in df.iterrows():
        title    = str(row["title"])[:500]
        text     = str(row["description"])
        case_type = classify_case_type(title, text)
        verdict  = str(row["disposal_nature"]) if pd.notna(row["disposal_nature"]) else None
        court_name = str(row["court"]) if pd.notna(row["court"]) else None
        cnr      = str(row["cnr"]) if pd.notna(row["cnr"]) else None

        cur.execute(
            """
            INSERT INTO cases
                (title, text, case_type, verdict, court_name, court_type, doc_url)
            VALUES (%s, %s, %s, %s, %s, 'High Court', %s)
            ON CONFLICT DO NOTHING
            """,
            (title, text, case_type, verdict, court_name, cnr),
        )
        inserted += cur.rowcount

    conn.commit()
    cur.close()
    return inserted


def update_tsvectors(conn, new_ids: list[int]) -> None:
    """Populate ts_vector for the freshly inserted rows in batches."""
    if not new_ids:
        return

    cur = conn.cursor()
    total = len(new_ids)
    done = 0

    for i in range(0, total, TSVEC_BATCH):
        batch = new_ids[i : i + TSVEC_BATCH]
        cur.execute(
            """
            UPDATE cases
            SET ts_vector = to_tsvector(
                'english',
                coalesce(title, '') || ' ' || left(coalesce(text, ''), 2000)
            )
            WHERE id = ANY(%s)
            """,
            (batch,),
        )
        conn.commit()
        done += len(batch)
        print(f"      ts_vector: {done}/{total}", end="\r", flush=True)

    cur.close()
    print(f"      ts_vector: {total}/{total} ✓          ")  # clear \r line


def get_new_ids(conn, before_max_id: int) -> list[int]:
    """Return IDs of rows inserted after the snapshot max id."""
    cur = conn.cursor()
    cur.execute("SELECT id FROM cases WHERE id > %s", (before_max_id,))
    ids = [r[0] for r in cur.fetchall()]
    cur.close()
    return ids


def current_max_id(conn) -> int:
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(MAX(id), 0) FROM cases")
    val = cur.fetchone()[0]
    cur.close()
    return val


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL is not set.", file=sys.stderr)
        print("  export DATABASE_URL='postgresql://user:pass@host/db?sslmode=require'")
        sys.exit(1)

    print("Connecting to Neon...")
    conn = psycopg2.connect(db_url)
    conn.autocommit = False

    grand_total = 0

    for court_name, court_code in COURTS.items():
        print(f"\n{'='*55}")
        print(f"  {court_name}  [{court_code}]")
        print(f"{'='*55}")
        court_total = 0

        for year in YEARS:
            print(f"\n  ▸ {year} ...", end=" ", flush=True)

            benches = list_benches(year, court_code)
            if not benches:
                print("no benches found, skipping.")
                continue

            # Use first available bench (principal bench)
            bench = benches[0]

            try:
                df_raw = fetch_parquet(year, court_code, bench)
            except urllib.error.HTTPError as e:
                print(f"HTTP {e.code}, skipping.")
                continue
            except Exception as e:
                print(f"fetch error ({e}), skipping.")
                continue

            df = sample_cases(df_raw)
            if df.empty:
                print("no usable rows, skipping.")
                continue

            print(f"{len(df)} rows sampled from {len(df_raw)} available.")

            # Snapshot max id before insert so we can find new rows
            snap_id = current_max_id(conn)

            inserted = insert_cases(conn, df)
            print(f"      inserted: {inserted} new rows")

            # Update ts_vector only for the rows we just added
            if inserted > 0:
                new_ids = get_new_ids(conn, snap_id)
                update_tsvectors(conn, new_ids)

            court_total += inserted
            time.sleep(0.5)   # gentle pause between years

        print(f"\n  {court_name} total: {court_total} rows inserted")
        grand_total += court_total

    conn.close()
    print(f"\n{'='*55}")
    print(f"  GRAND TOTAL inserted: {grand_total} rows")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
