import urllib.request
import pandas as pd
import psycopg2
import re
import os

DB_URL = os.environ.get("LOCAL_DB", "postgresql://localhost/legaldb")
BASE = "https://indian-high-court-judgments.s3.amazonaws.com/"
COURT_CODE = "7_26"
CASES_PER_YEAR = 1000

CASE_TYPE_KEYWORDS = {
    "criminal": ["cr.", "criminal", "bail", "fir", "murder", "theft", "ipc", "crpc"],
    "civil": ["civil", "suit", "decree", "injunction", "property", "contract"],
    "constitutional": ["writ", "article 226", "article 32", "mandamus", "certiorari", "habeas corpus"],
    "family": ["matrimonial", "divorce", "custody", "maintenance", "adoption"],
    "labour": ["labour", "labor", "workman", "industrial", "termination", "retrenchment"],
}

def classify_case_type(title, description):
    text = (title + " " + description).lower()
    scores = {ct: sum(1 for kw in kws if kw in text) for ct, kws in CASE_TYPE_KEYWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "general"

def get_benches(year):
    url = f"{BASE}?list-type=2&prefix=metadata/parquet/year={year}/court={COURT_CODE}/&delimiter=/"
    xml = urllib.request.urlopen(url).read().decode()
    return re.findall(r'bench=([^/]+)/', xml)

def fetch_year(year):
    benches = get_benches(year)
    if not benches:
        return pd.DataFrame()
    purl = f"{BASE}metadata/parquet/year={year}/court={COURT_CODE}/bench={benches[0]}/metadata.parquet"
    tmp = f"/tmp/delhi_{year}.parquet"
    urllib.request.urlretrieve(purl, tmp)
    df = pd.read_parquet(tmp, columns=["title","description","judge","disposal_nature","court","decision_date","cnr"])
    # Drop rows with empty description or title
    df = df[df["description"].str.strip().astype(bool)]
    df = df[df["title"].str.strip().astype(bool)]
    # Sample up to CASES_PER_YEAR
    if len(df) > CASES_PER_YEAR:
        df = df.sample(n=CASES_PER_YEAR, random_state=42)
    return df

def insert_cases(conn, rows):
    cur = conn.cursor()
    inserted = 0
    for _, row in rows.iterrows():
        title = str(row["title"])[:500]
        text = str(row["description"])
        case_type = classify_case_type(title, text)
        verdict = str(row["disposal_nature"]) if pd.notna(row["disposal_nature"]) else None
        court_name = str(row["court"])
        decision_date = row["decision_date"].date() if pd.notna(row["decision_date"]) else None
        cnr = str(row["cnr"]) if pd.notna(row["cnr"]) else None

        cur.execute("""
            INSERT INTO cases (title, text, case_type, verdict, court_name, court_type, doc_url)
            VALUES (%s, %s, %s, %s, %s, 'High Court', %s)
            ON CONFLICT DO NOTHING
        """, (title, text, case_type, verdict, court_name, cnr))
        inserted += cur.rowcount
    conn.commit()
    cur.close()
    return inserted

def main():
    # List available years
    url = f"{BASE}?list-type=2&prefix=metadata/parquet/&delimiter=/"
    xml = urllib.request.urlopen(url).read().decode()
    years = sorted(re.findall(r'year=(\d+)', xml))
    print(f"Years found: {years}")

    conn = psycopg2.connect(DB_URL)
    total = 0

    for year in years:
        print(f"\nFetching Delhi HC {year}...")
        try:
            df = fetch_year(year)
            if df.empty:
                print(f"  No data for {year}")
                continue
            print(f"  Fetched {len(df)} rows")
            n = insert_cases(conn, df)
            print(f"  Inserted {n} rows")
            total += n
        except Exception as e:
            print(f"  Error {year}: {e}")

    conn.close()
    print(f"\nDone. Total inserted: {total}")

if __name__ == "__main__":
    main()
