# ============================================================
# Nyay Khoj — BERT Case Type Predictor (CSV output)
# No DB writes — saves predictions to case_type_predictions.csv
# Run: python predict_case_types.py
# ============================================================

import os
import json
import csv
import torch
import psycopg2
from transformers import AutoTokenizer, AutoModelForSequenceClassification
from dotenv import load_dotenv

load_dotenv()

MODEL_PATH = "./nyay_khoj_legal_classifier"
BATCH_SIZE = 32
OUTPUT_CSV = "./case_type_predictions.csv"
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

# ── Fetch all cases ─────────────────────────────────────────
print("Connecting to DB...")
conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()
cur.execute("SELECT id, title, COALESCE(text, '') FROM cases ORDER BY id")
rows = cur.fetchall()
cur.close()
conn.close()
print(f"Fetched {len(rows)} cases")

# ── Predict and write to CSV ────────────────────────────────
with open(OUTPUT_CSV, "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["id", "predicted_case_type"])

    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i+BATCH_SIZE]
        ids    = [r[0] for r in batch]
        titles = [r[1] or "" for r in batch]
        texts  = [r[2] for r in batch]

        predictions = predict_batch(titles, texts)

        for case_id, pred in zip(ids, predictions):
            writer.writerow([case_id, pred])

        if (i // BATCH_SIZE) % 10 == 0:
            print(f"Predicted {min(i+BATCH_SIZE, len(rows))}/{len(rows)}...")

print(f"\nDone! Saved to {OUTPUT_CSV}")
