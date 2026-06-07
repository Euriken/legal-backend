from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg2
from sentence_transformers import SentenceTransformer
from groq import Groq
import os

app = Flask(__name__)
CORS(app)
model = SentenceTransformer("BAAI/bge-base-en-v1.5")
groq_client =groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
get_explanation(query, case_title, case_text, verdict, ipc_sections):
    prompt = f"""You are a legal assistant. A user searched for: "{query}"
    
A relevant Indian court case was found:
Title: {case_title}
Verdict: {verdict}
IPC Sections: {ipc_sections}
Summary: {case_text[:300]}

In 2-3 sentences, explain why this case is relevant to the user's query. Be concise and clear."""

    response = groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=150
    )
    return response.choices[0].message.content

def get_cases(query):
    conn = psycopg2.connect(dbname="legaldb", user="devanshgoel", password="", host="localhost", port="5432")
    cur = conn.cursor()
    prefixed_query = f"Represent this sentence for searching relevant passages: {query}"
    embedding = model.encode(prefixed_query, normalize_embeddings=True).tolist()
    cur.execute("""
        SELECT title, court_name, case_type, doc_url, text,
               1 - (embedding <=> %s::vector) AS similarity, ipc_sections, verdict
        FROM cases
        ORDER BY (1 - (embedding <=> %s::vector)) + 
                 CASE WHEN LOWER(case_type) LIKE '%%criminal%%' THEN 0.08 ELSE 0 END DESC
        LIMIT 5
    """, (embedding, embedding))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    results = []
    for r in rows:
        explanation = get_explanation(query, r[0], r[4], r[7], r[6])
        results.append({
            "title": r[0],
            "court": r[1],
            "case_type": r[2],
            "url": r[3],
            "text": r[4][:500],
            "similarity": round(r[5], 3),
            "ipc_sections": r[6],
            "verdict": r[7],
            "explanation": explanation
        })
    return results

@app.route("/")
def index():
    return "Indian Legal Search API is running!"

@app.route("/search", methods=["POST"])
def search():
    query = request.json.get("query", "")
    return jsonify(get_cases(query))

if __name__ == "__main__":
    app.run(debug=True, port=5000)