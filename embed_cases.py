import psycopg2
import os
from sentence_transformers import SentenceTransformer

DB_URL = "postgresql://localhost/legaldb"
MODEL_NAME = "BAAI/bge-base-en-v1.5"
BATCH_SIZE = 64

print("Loading embedding model...")
model = SentenceTransformer(MODEL_NAME)
print("Model loaded!")

conn = psycopg2.connect(DB_URL)
cur = conn.cursor()

cur.execute("SELECT COUNT(*) FROM cases WHERE embedding IS NULL")
total = cur.fetchone()[0]
print(f"Cases needing embeddings: {total}")

cur.execute("SELECT id, text FROM cases WHERE embedding IS NULL ORDER BY id")
rows = cur.fetchall()

for i in range(0, len(rows), BATCH_SIZE):
    batch = rows[i:i+BATCH_SIZE]
    ids = [r[0] for r in batch]
    texts = [r[1][:512] if r[1] else "" for r in batch]
    
    embeddings = model.encode(texts, normalize_embeddings=True)
    
    update_cur = conn.cursor()
    for j, (row_id, emb) in enumerate(zip(ids, embeddings)):
        update_cur.execute(
            "UPDATE cases SET embedding = %s WHERE id = %s",
            (emb.tolist(), row_id)
        )
    conn.commit()
    update_cur.close()
    
    done = min(i + BATCH_SIZE, total)
    print(f"  Embedded {done}/{total} cases")

conn.close()
print("Done! All embeddings generated.")
