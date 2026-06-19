from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg2
from sentence_transformers import SentenceTransformer
from groq import Groq
import os

from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
CORS(app)

model = SentenceTransformer("BAAI/bge-base-en-v1.5")
groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

CRIMINAL_KEYWORDS = {
    "murder", "theft", "robbery", "assault", "rape", "kidnap", "fraud",
    "cheating", "forgery", "extortion", "bribery", "criminal", "ipc",
    "arrest", "bail", "accused", "fir", "police", "offence", "offense",
    "conviction", "acquittal", "homicide", "dacoity", "abduction"
}

def is_criminal_query(query):
    words = query.lower().split()
    return any(w in CRIMINAL_KEYWORDS for w in words)

def get_cases(query):
    conn = psycopg2.connect(dbname="legaldb", user="devanshgoel", password="", host="localhost", port="5432")
    cur = conn.cursor()
    prefixed_query = "Represent this sentence for searching relevant passages: " + query
    embedding = model.encode(prefixed_query, normalize_embeddings=True).tolist()
    boost = 0.03 if is_criminal_query(query) else 0

    cur.execute("SELECT id, title, court_name, case_type, doc_url, text, 1 - (embedding <=> %s::vector) AS similarity, ipc_sections, verdict FROM cases ORDER BY similarity DESC LIMIT 20", (embedding,))
    vector_rows = cur.fetchall()

    try:
        cur.execute("SELECT id, title, court_name, case_type, doc_url, text, ts_rank(ts_vector, plainto_tsquery(%s, %s)) AS similarity, ipc_sections, verdict FROM cases WHERE ts_vector @@ plainto_tsquery(%s, %s) ORDER BY similarity DESC LIMIT 20", ("english", query, "english", query))
        bm25_rows = cur.fetchall()
    except Exception:
        bm25_rows = []

    cur.close()
    conn.close()

    K = 60
    scores = {}
    case_data = {}

    for rank, row in enumerate(vector_rows):
        cid = row[0]
        scores[cid] = scores.get(cid, 0) + 1.0 / (K + rank + 1)
        case_data[cid] = row

    for rank, row in enumerate(bm25_rows):
        cid = row[0]
        scores[cid] = scores.get(cid, 0) + 1.0 / (K + rank + 1)
        case_data[cid] = row

    def final_score(cid):
        criminal = case_data[cid][3] and "criminal" in case_data[cid][3].lower()
        return scores[cid] + (boost if criminal else 0)

    sorted_ids = sorted(scores.keys(), key=final_score, reverse=True)[:5]

    # Build a quick lookup of vector similarity scores
    vector_sim = {row[0]: round(row[6], 3) for row in vector_rows}

    results = []
    for cid in sorted_ids:
        r = case_data[cid]
        results.append({
            "title": r[1], "court": r[2], "case_type": r[3],
            "url": r[4], "text": r[5][:500],
            "similarity": vector_sim.get(cid, round(scores[cid], 4)),
            "ipc_sections": r[7], "verdict": r[8]
        })
    return results

@app.route("/")
def index():
    return "Indian Legal Search API is running!"

@app.route("/search", methods=["POST"])
def search():
    query = request.json.get("query", "")
    return jsonify(get_cases(query))

@app.route("/explain", methods=["POST"])
def explain():
    data = request.json
    query = data.get("query", "")
    case_title = data.get("title", "")
    case_text = data.get("text", "")
    verdict = data.get("verdict", "")
    ipc_sections = data.get("ipc_sections", "")

    prompt = f"""User searched: "{query}"
Case: {case_title} | Verdict: {verdict} | IPC: {ipc_sections}
Summary: {case_text[:200]}
In exactly 2 sentences, explain why this case is relevant. No intro, no filler."""

    response = groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=100
    )
    return jsonify({"explanation": response.choices[0].message.content})

@app.route("/chat", methods=["POST"])
def chat():
    user_message = request.json.get("message", "")
    history = request.json.get("history", [])

    messages = [
        {"role": "system", "content": """You are a concise Indian legal advisor. Reply in bullet points only — no paragraphs.
- List applicable IPC sections with a one-line explanation each
- State what legal action they can take in 1-2 bullets
- End with one line: "Consult a lawyer for actual advice."
Never write paragraphs. Keep total response under 100 words."""}
    ]

    for msg in history:
        messages.append({"role": msg["role"], "content": msg["content"]})

    messages.append({"role": "user", "content": user_message})

    response = groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=messages,
        max_tokens=200
    )

    return jsonify({"response": response.choices[0].message.content})

if __name__ == "__main__":
    app.run(debug=True, port=5000)