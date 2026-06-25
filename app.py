from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg2
from psycopg2 import pool
from sentence_transformers import SentenceTransformer
from groq import Groq
import os

from dotenv import load_dotenv
load_dotenv()

from ipc_bns_map import IPC_BNS_MAP

def enrich_ipc_sections(ipc_sections_str):
    if not ipc_sections_str:
        return {"bns_sections": "", "sentence_range": ""}
    
    sections = [s.strip() for s in ipc_sections_str.split(",") if s.strip()]
    bns_list = []
    mins = []
    maxs = []
    
    for s in sections:
        if s in IPC_BNS_MAP:
            entry = IPC_BNS_MAP[s]
            bns_list.append(entry["bns"])
            if entry["min"] not in ("Same as main offense", "None"):
                mins.append(entry["min"])
            if entry["max"] not in ("Same as main offense", "None"):
                maxs.append(entry["max"])
                
    if not mins or not maxs:
        for s in sections:
            if s in IPC_BNS_MAP:
                entry = IPC_BNS_MAP[s]
                mins.append(entry["min"])
                maxs.append(entry["max"])
                
    bns_sections = ", ".join(bns_list) if bns_list else ""
    
    if not mins or not maxs:
        return {"bns_sections": bns_sections, "sentence_range": ""}
        
    SEVERITY = {
        "None": 0,
        "Same as main offense": 0.1,
        "Fine": 0.5,
        "3 months": 1,
        "6 months": 2,
        "1 month": 0.9,
        "1 year": 3,
        "2 years": 4,
        "3 years": 5,
        "5 years": 6,
        "7 years": 7,
        "10 years": 8,
        "20 years": 9,
        "Life Imprisonment": 10,
        "Death or Life Imprisonment": 11,
        "Death": 12
    }
    
    def get_score(val):
        v = val.strip()
        if v in SEVERITY:
            return SEVERITY[v]
        v_lower = v.lower()
        if "death" in v_lower:
            return 12
        if "life" in v_lower:
            return 10
        if "20 year" in v_lower:
            return 9
        if "10 year" in v_lower:
            return 8
        if "7 year" in v_lower:
            return 7
        if "5 year" in v_lower:
            return 6
        if "3 year" in v_lower:
            return 5
        if "2 year" in v_lower:
            return 4
        if "1 year" in v_lower:
            return 3
        if "6 month" in v_lower:
            return 2
        if "3 month" in v_lower:
            return 1
        if "1 month" in v_lower:
            return 0.9
        if "fine" in v_lower:
            return 0.5
        if "same as" in v_lower:
            return 0.1
        return 0

    sorted_mins = sorted(mins, key=get_score)
    sorted_maxs = sorted(maxs, key=get_score, reverse=True)
    
    min_sentence = sorted_mins[0]
    max_sentence = sorted_maxs[0]
    
    if min_sentence == max_sentence:
        sentence_range = min_sentence
    else:
        sentence_range = f"{min_sentence} — {max_sentence}"
        
    return {"bns_sections": bns_sections, "sentence_range": sentence_range}


app = Flask(__name__)
CORS(app)

model = SentenceTransformer("BAAI/bge-base-en-v1.5")
groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

# Connection pool (min 1 conn, max 10)
db_pool = pool.SimpleConnectionPool(
    1, 10,
    os.environ.get("DATABASE_URL", "dbname=legaldb user=devanshgoel host=localhost port=5432")
)

def _backfill_tsvector():
    """Populate ts_vector for any cases missing it (e.g. newly imported OpenNyaya cases).
    Runs once at startup in a background thread so it doesn't block requests."""
    import threading, time
    def _run():
        time.sleep(5)  # wait for pool to be fully ready
        try:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM cases WHERE ts_vector IS NULL")
            missing = cur.fetchone()[0]
            if missing > 0:
                print(f"[startup] Backfilling ts_vector for {missing} cases...")
                cur.execute("""
                    UPDATE cases
                    SET ts_vector = to_tsvector('english',
                        coalesce(title, '') || ' ' || coalesce(text, ''))
                    WHERE ts_vector IS NULL
                """)
                conn.commit()
                print(f"[startup] ts_vector backfill complete ({missing} rows updated)")
            else:
                print("[startup] ts_vector already populated for all cases")
            cur.close()
            put_conn(conn)
        except Exception as e:
            print(f"[startup] ts_vector backfill failed: {e}")
    threading.Thread(target=_run, daemon=True).start()

_backfill_tsvector()

def get_conn():
    return db_pool.getconn()

def put_conn(conn):
    db_pool.putconn(conn)

CRIMINAL_KEYWORDS = {
    "murder", "theft", "robbery", "assault", "rape", "kidnap", "fraud",
    "cheating", "forgery", "extortion", "bribery", "criminal", "ipc",
    "arrest", "bail", "accused", "fir", "police", "offence", "offense",
    "conviction", "acquittal", "homicide", "dacoity", "abduction"
}

def is_criminal_query(query):
    words = query.lower().split()
    return any(w in CRIMINAL_KEYWORDS for w in words)

def get_cases(query, page=1, per_page=10, year_from=None, year_to=None, verdict=None, case_type=None, court=None):
    conn = get_conn()
    try:
        cur = conn.cursor()
        prefixed_query = "Represent this sentence for searching relevant passages: " + query
        embedding = model.encode(prefixed_query, normalize_embeddings=True).tolist()
        boost = 0.03 if is_criminal_query(query) else 0

        # Build filters where clause
        where_clauses = []
        vector_params = [embedding]
        
        if year_from is not None:
            where_clauses.append("case_year >= %s")
            vector_params.append(year_from)
        if year_to is not None:
            where_clauses.append("case_year <= %s")
            vector_params.append(year_to)
        if verdict:
            where_clauses.append("verdict = %s")
            vector_params.append(verdict)
        if case_type:
            where_clauses.append("case_type = %s")
            vector_params.append(case_type)
        if court:
            if court == "Supreme Court":
                where_clauses.append("court_type IN ('Supreme Court', 'Supreme_Court')")
            elif court == "High Court":
                where_clauses.append("court_type = 'High_Court'")
            elif court == "District Court / Tribunals":
                where_clauses.append("court_type = 'District_And_Tribunals'")
            else:
                where_clauses.append("court_name = %s")
                vector_params.append(court)
            
        where_str = ""
        if where_clauses:
            where_str = "WHERE " + " AND ".join(where_clauses)

        vector_query = f"""
            SELECT id, title, court_name, case_type, doc_url, text, 
                   1 - (embedding <=> %s::vector) AS similarity, 
                   ipc_sections, verdict, case_year, summary 
            FROM cases {where_str} 
            ORDER BY similarity DESC LIMIT 100
        """
        cur.execute(vector_query, vector_params)
        vector_rows = cur.fetchall()

        try:
            bm25_params = ["english", query, "english", query]
            bm25_clauses = ["ts_vector @@ plainto_tsquery(%s, %s)"]
            if year_from is not None:
                bm25_clauses.append("case_year >= %s")
                bm25_params.append(year_from)
            if year_to is not None:
                bm25_clauses.append("case_year <= %s")
                bm25_params.append(year_to)
            if verdict:
                bm25_clauses.append("verdict = %s")
                bm25_params.append(verdict)
            if case_type:
                bm25_clauses.append("case_type = %s")
                bm25_params.append(case_type)
            if court:
                if court == "Supreme Court":
                    bm25_clauses.append("court_type IN ('Supreme Court', 'Supreme_Court')")
                elif court == "High Court":
                    bm25_clauses.append("court_type = 'High_Court'")
                elif court == "District Court / Tribunals":
                    bm25_clauses.append("court_type = 'District_And_Tribunals'")
                else:
                    bm25_clauses.append("court_name = %s")
                    bm25_params.append(court)
                
            bm25_where = "WHERE " + " AND ".join(bm25_clauses)
            bm25_query = f"""
                SELECT id, title, court_name, case_type, doc_url, text, 
                       ts_rank(ts_vector, plainto_tsquery(%s, %s)) AS similarity, 
                       ipc_sections, verdict, case_year, summary 
                FROM cases {bm25_where} 
                ORDER BY similarity DESC LIMIT 100
            """
            cur.execute(bm25_query, bm25_params)
            bm25_rows = cur.fetchall()
        except Exception:
            bm25_rows = []

        cur.close()
    finally:
        put_conn(conn)

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

    all_sorted = sorted(scores.keys(), key=final_score, reverse=True)
    total = len(all_sorted)
    start = (page - 1) * per_page
    page_ids = all_sorted[start:start + per_page]

    # Build a quick lookup of vector similarity scores
    vector_sim = {row[0]: round(row[6], 3) for row in vector_rows}

    results = []
    for cid in page_ids:
        r = case_data[cid]
        ipc = r[7]
        enriched = enrich_ipc_sections(ipc)
        results.append({
            "id": r[0],
            "title": r[1], "court": r[2], "case_type": r[3],
            "url": r[4], "text": r[5][:500],
            "similarity": vector_sim.get(cid, round(scores[cid], 4)),
            "ipc_sections": ipc, 
            "verdict": r[8],
            "bns_sections": enriched["bns_sections"],
            "sentence_range": enriched["sentence_range"],
            "year": r[9],
            "summary": r[10]
        })
    return {"results": results, "total": total, "page": page, "per_page": per_page}

@app.route("/")
def index():
    return "Indian Legal Search API is running!"

@app.route("/search", methods=["POST"])
def search():
    try:
        query = request.json.get("query", "")
        page = request.json.get("page", 1)
        per_page = request.json.get("per_page", 10)
        year_from = request.json.get("year_from")
        year_to = request.json.get("year_to")
        verdict = request.json.get("verdict")
        case_type = request.json.get("case_type")
        court = request.json.get("court")
        
        # Parse years as optional integers
        if year_from is not None:
            try:
                year_from = int(year_from)
            except ValueError:
                year_from = None
        if year_to is not None:
            try:
                year_to = int(year_to)
            except ValueError:
                year_to = None

        return jsonify(get_cases(query, page, per_page, year_from, year_to, verdict, case_type, court))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/case/<int:case_id>", methods=["GET"])
def get_case(case_id):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, title, court_name, case_type, doc_url, text, ipc_sections, verdict, case_year, summary FROM cases WHERE id = %s", (case_id,))
        row = cur.fetchone()
        if not row:
            cur.close()
            return jsonify({"error": "Case not found"}), 404
            
        summary = row[9]
        if not summary:
            # Generate summary on-the-fly
            try:
                title = row[1]
                text = row[5]
                snippet = text[:3000] if text else ""
                prompt = f"""You are a helpful assistant summarizing Indian court judgments.
Summarize the following judgment in exactly 2-3 sentences. Do not use formatting like bullet points, markdown headers, or intro phrases. Provide a clean, single paragraph.

Case: {title}
Judgment Snippet:
{snippet}
"""
                response = groq_client.chat.completions.create(
                    model="llama-3.1-8b-instant",
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=150,
                    temperature=0.3
                )
                summary = response.choices[0].message.content.strip()
                
                # Update database
                cur.execute("UPDATE cases SET summary = %s WHERE id = %s", (summary, case_id))
                conn.commit()
            except Exception as e:
                # Fallback to empty if LLM fails
                summary = ""
                
        cur.close()
        ipc = row[6]
        enriched = enrich_ipc_sections(ipc)
        return jsonify({
            "id": row[0], "title": row[1], "court": row[2], "case_type": row[3],
            "url": row[4], "text": row[5], "ipc_sections": ipc, "verdict": row[7],
            "bns_sections": enriched["bns_sections"], "sentence_range": enriched["sentence_range"],
            "year": row[8], "summary": summary
        })
    finally:
        put_conn(conn)

@app.route("/related/<int:case_id>", methods=["GET"])
def get_related(case_id):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, title, court_name, case_type, doc_url, ipc_sections, verdict, 1 - (embedding <=> (SELECT embedding FROM cases WHERE id = %s)) AS similarity, case_year, summary FROM cases WHERE id != %s AND embedding IS NOT NULL ORDER BY similarity DESC LIMIT 5", (case_id, case_id))
        rows = cur.fetchall()
        cur.close()
        results = []
        for r in rows:
            enriched = enrich_ipc_sections(r[5])
            results.append({
                "id": r[0], "title": r[1], "court": r[2], "case_type": r[3],
                "url": r[4], "ipc_sections": r[5], "verdict": r[6],
                "similarity": round(r[7], 3),
                "bns_sections": enriched["bns_sections"], "sentence_range": enriched["sentence_range"],
                "year": r[8], "summary": r[9]
            })
        return jsonify(results)
    finally:
        put_conn(conn)

@app.route("/explain", methods=["POST"])
def explain():
    try:
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
    except Exception as e:
        return jsonify({"explanation": "Could not generate explanation.", "error": str(e)}), 500

@app.route("/chat", methods=["POST"])
def chat():
    try:
        user_message = request.json.get("message", "")
        history = request.json.get("history", [])

        messages = [
            {"role": "system", "content": """You are a concise Indian legal advisor. Reply in bullet points only — no paragraphs.
- List applicable IPC sections with their BNS (Bharatiya Nyaya Sanhita) equivalents and a one-line explanation each
- State what legal action they can take in 1-2 bullets
- End with one line: "Consult a lawyer for actual advice."
Never write paragraphs. Keep total response under 150 words."""}
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
    except Exception as e:
        return jsonify({"error": str(e), "response": "Sorry, I encountered an error processing your request. Please try again."}), 500

@app.route("/stats", methods=["GET"])
def stats():
    conn = get_conn()
    try:
        cur = conn.cursor()
        
        # 1. Cases by Court
        cur.execute("SELECT court_name, COUNT(*) FROM cases GROUP BY court_name ORDER BY COUNT(*) DESC LIMIT 5")
        courts = [{"court": r[0], "count": r[1]} for r in cur.fetchall()]
        
        # 2. Cases by Type
        cur.execute("SELECT case_type, COUNT(*) FROM cases WHERE case_type IS NOT NULL AND case_type != '' GROUP BY case_type ORDER BY COUNT(*) DESC")
        types = [{"type": r[0], "count": r[1]} for r in cur.fetchall()]
        
        # 3. Cases by Year (1950 - 2026)
        cur.execute("SELECT case_year, COUNT(*) FROM cases WHERE case_year IS NOT NULL AND case_year BETWEEN 1950 AND 2026 GROUP BY case_year ORDER BY case_year")
        years = [{"year": r[0], "count": r[1]} for r in cur.fetchall()]
        
        # 4. Top IPC Sections
        cur.execute("""
            SELECT trim(sec), COUNT(*) 
            FROM (
                SELECT unnest(string_to_array(ipc_sections, ',')) AS sec 
                FROM cases 
                WHERE ipc_sections IS NOT NULL AND ipc_sections != ''
            ) AS sub 
            GROUP BY trim(sec) 
            ORDER BY COUNT(*) DESC 
            LIMIT 10
        """)
        ipc = [{"section": r[0], "count": r[1]} for r in cur.fetchall()]
        
        # 5. Verdict Distribution
        cur.execute("SELECT verdict, COUNT(*) FROM cases WHERE verdict IS NOT NULL AND verdict != '' GROUP BY verdict ORDER BY COUNT(*) DESC")
        verdicts = [{"verdict": r[0], "count": r[1]} for r in cur.fetchall()]
        
        # 6. Total Count
        cur.execute("SELECT COUNT(*) FROM cases")
        total = cur.fetchone()[0]
        
        cur.close()
        
        return jsonify({
            "total_cases": total,
            "by_court": courts,
            "by_type": types,
            "by_year": years,
            "by_ipc": ipc,
            "by_verdict": verdicts
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        put_conn(conn)

if __name__ == "__main__":
    app.run(debug=True, port=5001)