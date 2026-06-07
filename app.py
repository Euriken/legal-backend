from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg2
from sentence_transformers import SentenceTransformer
from groq import Groq
import os

app = Flask(__name__)
CORS(app)
model = SentenceTransformer("BAAI/bge-base-en-v1.5")
groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

def get_explanation(query, case_title, case_text, verdict, ipc_sections):
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
                 CASE WHEN LOWER(case_type) LIKE '%%criminal%%' THEN 0.03 ELSE 0 END DESC
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

@app.route("/chat", methods=["POST"])
def chat():
    user_message = request.json.get("message", "")
    history = request.json.get("history", [])
    
    messages = [
        {"role": "system", "content": """You are an expert Indian legal advisor. 
When a user describes their situation, you:
1. Identify applicable IPC sections with section numbers
2. Explain what each section means in simple terms
3. Suggest what legal action they can take
4. Keep responses clear and helpful for a common person
Always mention that they should consult a real lawyer for actual legal advice."""}
    ]
    
    for msg in history:
        messages.append({"role": msg["role"], "content": msg["content"]})
    
    messages.append({"role": "user", "content": user_message})
    
    response = groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=messages,
        max_tokens=500
    )
    
    return jsonify({"response": response.choices[0].message.content})

if __name__ == "__main__":
    app.run(debug=True, port=5000)