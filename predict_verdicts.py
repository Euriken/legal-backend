import os, json, csv, torch, psycopg2
from transformers import AutoTokenizer, AutoModelForSequenceClassification
from dotenv import load_dotenv
load_dotenv()

MODEL_PATH = "./nyay_khoj_verdict_classifier"
BATCH_SIZE = 32
OUTPUT_CSV = "./verdict_predictions.csv"
DATABASE_URL = os.getenv("DATABASE_URL")

tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH)
model = AutoModelForSequenceClassification.from_pretrained(MODEL_PATH)
model.eval()
device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
model = model.to(device)

with open(f"{MODEL_PATH}/label_classes.json") as f:
    label_classes = json.load(f)

def predict_batch(titles, texts):
    inputs = [t + " [SEP] " + x[:800] for t, x in zip(titles, texts)]
    tokens = tokenizer(inputs, truncation=True, padding="max_length", max_length=512, return_tensors="pt")
    tokens = {k: v.to(device) for k, v in tokens.items()}
    with torch.no_grad():
        logits = model(**tokens).logits
    return [label_classes[p] for p in logits.argmax(-1).tolist()]

conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()
cur.execute("SELECT id, title, COALESCE(text, '') FROM cases ORDER BY id")
rows = cur.fetchall()
cur.close()
conn.close()
print(f"Fetched {len(rows)} cases")

with open(OUTPUT_CSV, "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["id", "predicted_verdict"])
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i+BATCH_SIZE]
        preds = predict_batch([r[1] or "" for r in batch], [r[2] for r in batch])
        for case_id, pred in zip([r[0] for r in batch], preds):
            writer.writerow([case_id, pred])
        if (i // BATCH_SIZE) % 10 == 0:
            print(f"Predicted {min(i+BATCH_SIZE, len(rows))}/{len(rows)}...")

print(f"Done! Saved to {OUTPUT_CSV}")
