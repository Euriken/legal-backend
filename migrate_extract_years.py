import os
import re
import psycopg2
from dotenv import load_dotenv

# Load env variables
load_dotenv(dotenv_path="/Users/devanshgoel/legal-backend/.env")

MONTHS = r"(?:january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)"

regex1 = re.compile(rf"{MONTHS}\s*\d{{1,2}}\s*[,\s]\s*([0-9Il|ioO\\\/ ]{{4,6}})", re.IGNORECASE)
regex2 = re.compile(rf"\d{{1,2}}\s*{MONTHS}\s*[,\s]\s*([0-9Il|ioO\\\/ ]{{4,6}})", re.IGNORECASE)
regex3 = re.compile(rf"{MONTHS}\s*[,\s]\s*([0-9Il|ioO\\\/ ]{{4,6}})", re.IGNORECASE)
regex_year_fallback = re.compile(r"\b([12][0-9Il|ioO\\\/]{3})\b")

def clean_year(yr_str):
    if not yr_str:
        return None
    s = yr_str.replace('\\', '').replace('/', '').replace(' ', '').replace(',', '').replace('.', '')
    s = s.replace('I', '1').replace('l', '1').replace('|', '1').replace('i', '1')
    s = s.replace('o', '0').replace('O', '0')
    s = re.sub(r'[^0-9]', '', s)
    if len(s) == 4:
        val = int(s)
        if 1800 <= val <= 2026:
            return val
    return None

def extract_year(title, text):
    if not text:
        text = ""
    if not title:
        title = ""
        
    snippet = text[:1500]
    
    m1 = regex1.search(snippet)
    if m1:
        val = clean_year(m1.group(1))
        if val: return val
        
    m2 = regex2.search(snippet)
    if m2:
        val = clean_year(m2.group(1))
        if val: return val
        
    m3 = regex3.search(snippet)
    if m3:
        val = clean_year(m3.group(1))
        if val: return val
        
    # Search for year in title
    for token in re.split(r'\s+', title):
        val = clean_year(token)
        if val: return val
        
    # Fallback: search for any 19xx or 20xx-like token in text
    for m in regex_year_fallback.finditer(snippet):
        val = clean_year(m.group(1))
        if val: return val
        
    return None

def main():
    db_url = os.environ.get("DATABASE_URL", "dbname=legaldb user=devanshgoel host=localhost port=5432")
    print(f"Connecting to {db_url}...")
    
    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    cur = conn.cursor()
    
    try:
        # Add column if not exists
        print("Ensuring 'case_year' column exists...")
        cur.execute("ALTER TABLE cases ADD COLUMN IF NOT EXISTS case_year INTEGER;")
        conn.commit()
        
        # Fetch all cases (we can do it in cursor server-side or pull all IDs, titles, and text snippets to save memory)
        # To avoid pulling huge texts for 37k rows, we only SELECT id, title, substring(text from 1 for 1500)
        print("Fetching cases...")
        cur.execute("SELECT id, title, substring(text from 1 for 1500) FROM cases ORDER BY id;")
        rows = cur.fetchall()
        print(f"Loaded {len(rows)} cases. Extracting years...")
        
        updates = []
        count = 0
        missing = 0
        
        for cid, title, snippet in rows:
            year = extract_year(title, snippet)
            if year:
                updates.append((year, cid))
                count += 1
            else:
                missing += 1
                
        print(f"Year extraction complete. Extracted: {count}, Missing: {missing}")
        
        # Batch updates
        batch_size = 1000
        print("Executing batch updates...")
        for i in range(0, len(updates), batch_size):
            batch = updates[i:i+batch_size]
            # Execute update
            cur.executemany("UPDATE cases SET case_year = %s WHERE id = %s;", batch)
            conn.commit()
            print(f"Updated {min(i + batch_size, len(updates))} / {len(updates)} cases...")
            
        print("Migration completed successfully!")
    except Exception as e:
        conn.rollback()
        print(f"Migration failed: {e}")
    finally:
        cur.close()
        conn.close()

if __name__ == "__main__":
    main()
