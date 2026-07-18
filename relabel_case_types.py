# ============================================================
# Nyay Khoj — Batch Case Type Re-labeling Script
# Uses fine-tuned BERT to update case_type in Neon DB
# Run once from your legal-backend directory:
#   python relabel_case_types.py
# ============================================================

import os
import json
import torch
import psycopg2
from transformers import AutoTokenizer, AutoModelForSequenceClassification
from dotenv import load_dotenv

load_dotenv()

# ── Config ──────────────────────────────────────────────────
MODEL_PATH = "./nyay_khoj_legal_classifier"
BATCH_SIZE = 32
DATABASE_URL = os.getenv("DATABASE_URL")

# ── Load model ──────────────────────────────────────────────
print("Loading BERT classifier...")
tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH)
model = AutoModelForSequenceClassification.from_pretrained(MODEL_PATH)
model.eval()

device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
model = model.to(device)
print(f"Model loaded on {device}")

with open(f"{MODEL_PATH}/label_classes.json") as f:
    label_classes = json.load(f)
print(f"Classes: {label_classes}")

# ── Predict function ────────────────────────────────────────
def predict_batch(titles, texts):
    inputs_text = [
        t + " [SEP] " + x[:800]
        for t, x in zip(titles, texts)
    ]
    tokens = tokenizer(
        inputs_text,
        truncation=True,
        padding="max_length",
        max_length=512,
        return_tensors="pt"
    )
    tokens = {k: v.to(device) for k, v in tokens.items()}
    with torch.no_grad():
        logits = model(**tokens).logits
    preds = logits.argmax(-1).tolist()
    return [label_classes[p] for p in preds]

# ── Connect to Neon ─────────────────────────────────────────
print("Connecting to database...")
conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()

# Fetch all cases
cur.execute("SELECT id, title, COALESCE(text, '') FROM cases")
rows = cur.fetchall()
print(f"Fetched {len(rows)} cases")

# ── Batch predict and update ────────────────────────────────
updated = 0
errors = 0

for i in range(0, len(rows), BATCH_SIZE):
    batch = rows[i:i+BATCH_SIZE]
    ids    = [r[0] for r in batch]
    titles = [r[1] or "" for r in batch]
    texts  = [r[2] for r in batch]

    try:
        predictions = predict_batch(titles, texts)
        for case_id, pred in zip(ids, predictions):
            cur.execute(
                "UPDATE cases SET case_type = %s WHERE id = %s",
                (pred, case_id)
            )
        conn.commit()
        updated += len(batch)
        print(f"Updated {updated}/{len(rows)} cases... last batch: {predictions[:3]}")
    except Exception as e:
        print(f"Error at batch {i}: {e}")
        conn.rollback()
        errors += len(batch)

cur.close()
conn.close()

print(f"\nDone! Updated: {updated} | Errors: {errors}")
print("Run a SELECT case_type, COUNT(*) FROM cases GROUP BY case_type to verify.")

