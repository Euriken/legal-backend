import psycopg2
import os

DB_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/legaldb")

try:
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    
    print("Normalizing court names in database...")
    cur.execute("UPDATE cases SET court_name = 'High Court of Delhi' WHERE court_name = 'Delhi High Court'")
    updated_rows = cur.rowcount
    conn.commit()
    print(f"Migration complete: {updated_rows} cases updated to 'High Court of Delhi'")
    
    cur.close()
    conn.close()
except Exception as e:
    print("Error executing migration:", e)
