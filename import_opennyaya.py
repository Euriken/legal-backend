import os
import re
import psycopg2
import pathlib
import sys

DB_URL = "postgresql://localhost/legaldb"

KEYWORDS = {
    "Criminal": ["ipc", "indian penal code", "murder", "theft", "fraud", "criminal", "accused", "prosecution", "crpc", "bail", "acquit", "convict"],
    "Constitution": ["article 14", "article 21", "article 32", "fundamental right", "writ", "constitution", "constitutional", "directive principle"],
    "Tax": ["income tax", "tax act", "gst", "customs", "excise", "service tax", "revenue", "assessee"],
    "Motorvehicles": ["motor vehicle", "mv act", "accident", "compensation", "tribunal", "insurance claim", "road accident"],
    "Land&Property": ["land acquisition", "transfer of property", "rent", "tenancy", "lease", "eviction", "property", "land"],
    "Industrial&Labour": ["industrial dispute", "labour", "workmen", "factory", "trade union", "employee", "termination", "retrenchment"],
    "Financial": ["banking", "rbi", "securities", "company act", "insolvency", "nbfc", "sebi", "finance"],
    "Civil": ["civil", "contract", "specific relief", "limitation", "suit", "decree", "injunction", "damages"],
}

def classify(text):
    t = text.lower()
    scores = {}
    for cat, kws in KEYWORDS.items():
        scores[cat] = sum(1 for kw in kws if kw in t)
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "Civil"

def extract_verdict(text):
    t = text.lower()
    if "appeal allowed" in t or "petition allowed" in t:
        return "Appeal Allowed"
    elif "acquitted" in t:
        return "Acquitted"
    elif "convicted" in t:
        return "Convicted"
    elif "dismissed" in t:
        return "Dismissed"
    return "Dismissed"

def extract_ipc(text):
    sections = re.findall(r"[Ss]ection\s+(\d+[A-Za-z]*)\s+(?:of\s+)?(?:IPC|I\.P\.C|Indian Penal Code)", text)
    return ", ".join(set(sections[:5])) if sections else None

def get_existing_titles(conn):
    cur = conn.cursor()
    cur.execute("SELECT title FROM cases")
    titles = {row[0] for row in cur.fetchall()}
    cur.close()
    return titles

def main():
    data_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/legal-backend/opennyaya/nyai-supreme-court-clean")
    conn = psycopg2.connect(DB_URL)
    existing = get_existing_titles(conn)
    print(f"Existing cases: {len(existing)}")
    md_files = sorted(pathlib.Path(data_dir).rglob("*.md"))
    print(f"Found {len(md_files)} judgment files")
    inserted = 0
    skipped = 0
    for i, fpath in enumerate(md_files):
        try:
            text = fpath.read_text(encoding="utf-8", errors="ignore")
            if len(text) < 200:
                continue
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            title = lines[0][:300] if lines else str(fpath.name)
            if title in existing:
                skipped += 1
                continue
            full_text = text[:10000]
            case_type = classify(full_text)
            verdict = extract_verdict(full_text)
            ipc = extract_ipc(full_text) if case_type == "Criminal" else None
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO cases (title, text, case_type, verdict, court_name, court_type, ipc_sections)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (title, full_text, case_type, verdict, "Supreme Court of India", "Supreme Court", ipc))
            conn.commit()
            cur.close()
            existing.add(title)
            inserted += 1
            if inserted % 100 == 0:
                print(f"  Inserted {inserted} | Skipped {skipped} | File {i+1}/{len(md_files)}")
        except Exception as e:
            print(f"  Error on {fpath.name}: {e}")
            try: conn.rollback()
            except: pass
    conn.close()
    print(f"Done! Inserted: {inserted} | Skipped: {skipped}")

if __name__ == "__main__":
    main()
