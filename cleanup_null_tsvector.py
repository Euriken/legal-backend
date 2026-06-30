#!/usr/bin/env python3
"""
cleanup_null_tsvector.py — Delete rows where ts_vector IS NULL from Neon cases table.

Prints counts before deletion, requires y/n confirmation, then VACUUMs to free space.

Usage:
    DATABASE_URL="postgresql://..." python3 cleanup_null_tsvector.py
"""

import os
import sys
import psycopg2

def vacuum_table(conn):
    """VACUUM must run outside a transaction block."""
    conn.set_isolation_level(0)
    cur = conn.cursor()
    cur.execute("VACUUM cases")
    cur.close()
    conn.set_isolation_level(1)

def main():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL is not set.", file=sys.stderr)
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

    # ── Pre-deletion counts ───────────────────────────────────────────────────
    cur.execute("SELECT COUNT(*) FROM cases WHERE ts_vector IS NULL")
    null_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM cases WHERE ts_vector IS NOT NULL")
    good_count = cur.fetchone()[0]

    total = null_count + good_count

    print()
    print("=" * 50)
    print(f"  Total rows:              {total:>8,}")
    print(f"  ts_vector IS NOT NULL:   {good_count:>8,}  ← will be KEPT")
    print(f"  ts_vector IS NULL:       {null_count:>8,}  ← will be DELETED")
    print("=" * 50)
    print()

    if null_count == 0:
        print("Nothing to delete — no NULL ts_vector rows found.")
        conn.close()
        return

    # ── Confirmation prompt ───────────────────────────────────────────────────
    try:
        answer = input(f"Delete {null_count:,} rows where ts_vector IS NULL? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        conn.close()
        sys.exit(0)

    if answer != "y":
        print("Aborted — no rows deleted.")
        conn.close()
        sys.exit(0)

    # ── Delete ────────────────────────────────────────────────────────────────
    print(f"\nDeleting {null_count:,} rows...")
    try:
        cur.execute("DELETE FROM cases WHERE ts_vector IS NULL")
        deleted = cur.rowcount
        conn.commit()
        print(f"✅  Deleted {deleted:,} rows.")
    except Exception as e:
        conn.rollback()
        print(f"ERROR during DELETE: {e}", file=sys.stderr)
        conn.close()
        sys.exit(1)

    # ── VACUUM to reclaim space ───────────────────────────────────────────────
    print("Running VACUUM to reclaim space...")
    try:
        vacuum_table(conn)
        print("✅  VACUUM complete.")
    except Exception as e:
        print(f"⚠️  VACUUM warning (non-fatal): {e}")

    # ── Final count ───────────────────────────────────────────────────────────
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM cases")
    final_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM cases WHERE ts_vector IS NOT NULL")
    final_good = cur.fetchone()[0]

    print()
    print("=" * 50)
    print(f"  Final row count:         {final_count:>8,}")
    print(f"  Rows with ts_vector:     {final_good:>8,}")
    print("=" * 50)

    cur.close()
    conn.close()
    print("\nDone. Space should now be freed on Neon.")

if __name__ == "__main__":
    main()
