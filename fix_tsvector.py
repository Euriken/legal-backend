#!/usr/bin/env python3
"""
fix_tsvector.py — Incrementally populate ts_vector for NULL rows on Neon free tier.

Strategies to stay within 512MB:
  - Indexes only first 2000 chars of text (smaller tsvector per row)
  - VACUUMs the table after every batch to reclaim dead tuple space
  - Sleeps between batches to let Neon release space
  - On storage error: waits 10s and retries the batch at half size before giving up

Safe to run repeatedly — always picks up where it left off.

Usage:
    DATABASE_URL="postgresql://..." python3 fix_tsvector.py
    DATABASE_URL="postgresql://..." python3 fix_tsvector.py --batch-size 200 --sleep 3
    DATABASE_URL="postgresql://..." python3 fix_tsvector.py --limit 2000
"""

import os
import sys
import time
import argparse
import psycopg2
import psycopg2.extras

# Neon / Postgres storage-limit signals
STORAGE_ERROR_HINTS = [
    "could not extend file",
    "no space left",
    "out of shared memory",
    "53100",   # SQLSTATE: disk_full
    "53200",   # SQLSTATE: out_of_memory
    "54000",   # SQLSTATE: program_limit_exceeded
    "neon: storage",
    "relation size limit",
    "shared_buffers",
]

def is_storage_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(hint.lower() in msg for hint in STORAGE_ERROR_HINTS)


def parse_args():
    p = argparse.ArgumentParser(description="Populate ts_vector for NULL rows (Neon-safe)")
    p.add_argument("--batch-size", type=int, default=500,
                   help="Rows per commit batch (default: 500)")
    p.add_argument("--limit", type=int, default=0,
                   help="Stop after this many total rows this run (0 = no limit)")
    p.add_argument("--sleep", type=float, default=2.0,
                   help="Seconds to sleep between batches (default: 2)")
    return p.parse_args()


def do_update(cur, ids):
    """Run the tsvector UPDATE for a list of IDs. Returns rowcount."""
    cur.execute(
        """
        UPDATE cases
        SET ts_vector = to_tsvector(
            'english',
            coalesce(title, '') || ' ' || left(coalesce(text, ''), 2000)
        )
        WHERE id = ANY(%s)
        """,
        (ids,)
    )
    return cur.rowcount


def vacuum_table(conn):
    """VACUUM cases — must run outside a transaction block."""
    old_isolation = conn.isolation_level
    conn.set_isolation_level(0)          # autocommit required for VACUUM
    try:
        vac_cur = conn.cursor()
        vac_cur.execute("VACUUM cases")
        vac_cur.close()
    finally:
        conn.set_isolation_level(old_isolation)


def main():
    args = parse_args()
    batch_size = args.batch_size
    hard_limit = args.limit
    sleep_secs = args.sleep

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL environment variable is not set.", file=sys.stderr)
        print("  export DATABASE_URL='postgresql://user:pass@host/db?sslmode=require'")
        sys.exit(1)

    print("Connecting to Neon...")
    try:
        conn = psycopg2.connect(db_url)
        conn.autocommit = False
    except Exception as e:
        print(f"ERROR: Could not connect — {e}", file=sys.stderr)
        sys.exit(1)

    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM cases WHERE ts_vector IS NULL")
    total_null = cur.fetchone()[0]
    print(f"Rows with ts_vector = NULL: {total_null}")
    if total_null == 0:
        print("Nothing to do. All rows already have ts_vector populated.")
        conn.close()
        return

    target = min(total_null, hard_limit) if hard_limit else total_null
    print(f"Target this run: {target} rows  |  batch={batch_size}  sleep={sleep_secs}s")
    print()

    total_updated = 0
    batch_num = 0

    while True:
        # ── respect --limit ──────────────────────────────────────────────────
        if hard_limit and total_updated >= hard_limit:
            print(f"\nReached --limit of {hard_limit}. Stopping.")
            break

        fetch_size = batch_size
        if hard_limit:
            fetch_size = min(batch_size, hard_limit - total_updated)

        # ── fetch next batch of IDs ──────────────────────────────────────────
        cur.execute(
            "SELECT id FROM cases WHERE ts_vector IS NULL ORDER BY id LIMIT %s",
            (fetch_size,)
        )
        ids = [r[0] for r in cur.fetchall()]

        if not ids:
            print("\nAll done — no more NULL rows found.")
            break

        batch_num += 1

        # ── attempt UPDATE ───────────────────────────────────────────────────
        try:
            affected = do_update(cur, ids)
            conn.commit()
            total_updated += affected
            pct = (total_updated / target * 100) if target else 0
            print(f"  Batch {batch_num}: {affected} rows  [{total_updated}/{target}  {pct:.1f}%]")

        except psycopg2.Error as e:
            conn.rollback()

            if not is_storage_error(e):
                print(f"\nERROR on batch {batch_num}: {e}", file=sys.stderr)
                conn.close()
                raise

            # ── storage error → retry at half size ───────────────────────────
            half = max(1, len(ids) // 2)
            print(f"\n⚠️  Storage limit hit on batch {batch_num} ({len(ids)} rows).")
            print(f"   Waiting 10s then retrying at half size ({half} rows)...")
            time.sleep(10)

            try:
                retry_ids = ids[:half]
                affected = do_update(cur, retry_ids)
                conn.commit()
                total_updated += affected
                pct = (total_updated / target * 100) if target else 0
                print(f"   Retry OK: {affected} rows  [{total_updated}/{target}  {pct:.1f}%]")

            except psycopg2.Error as e2:
                conn.rollback()
                print(f"\n⛔  Retry also failed — Neon storage is truly full.")
                print(f"   {e2}")
                print(f"\n   Rows updated this run: {total_updated}")
                print(f"   Rows still remaining:  {total_null - total_updated}")
                print(f"\n   Free up space on Neon (delete old data, vacuum, or upgrade),")
                print(f"   then re-run. The script will pick up where it left off.")
                conn.close()
                sys.exit(1)

        # ── VACUUM to reclaim dead tuple space before next batch ─────────────
        try:
            vacuum_table(conn)
        except Exception as ve:
            # VACUUM failure is non-fatal — warn and continue
            print(f"   ⚠️  VACUUM warning: {ve}")

        # ── sleep to let Neon release space ──────────────────────────────────
        if sleep_secs > 0:
            time.sleep(sleep_secs)

    cur.close()
    conn.close()
    print(f"\nSession complete. Total rows updated: {total_updated}")


if __name__ == "__main__":
    main()
