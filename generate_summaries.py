import os
import sys
import time
import argparse
import psycopg2
from dotenv import load_dotenv
from groq import Groq

# Load environment
load_dotenv(dotenv_path="/Users/devanshgoel/nyay-khoj/backend/.env")

def main():
    parser = argparse.ArgumentParser(description="Batch generate summaries for cases using Groq LLM.")
    parser.add_argument("--limit", type=int, default=50, help="Number of cases to summarize in this run.")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay in seconds between API calls to avoid rate limits.")
    args = parser.parse_args()

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        print("Error: GROQ_API_KEY environment variable not set.")
        sys.exit(1)

    db_url = os.environ.get("DATABASE_URL", "dbname=legaldb user=devanshgoel host=localhost port=5432")
    conn = psycopg2.connect(db_url)
    cur = conn.cursor()

    try:
        # Get cases that don't have a summary
        cur.execute(
            "SELECT id, title, text FROM cases WHERE summary IS NULL OR summary = '' LIMIT %s",
            (args.limit,)
        )
        rows = cur.fetchall()
        
        if not rows:
            print("No cases found without a summary. Database is fully summarized!")
            return

        print(f"Found {len(rows)} cases to summarize. Connecting to Groq...")
        client = Groq(api_key=api_key)

        success_count = 0
        for i, (cid, title, text) in enumerate(rows):
            print(f"[{i+1}/{len(rows)}] Summarizing Case ID {cid}: {title[:60]}...")
            
            # Prepare snippet (max 3000 chars for context)
            snippet = text[:3000] if text else ""
            
            prompt = f"""You are a helpful assistant summarizing Indian court judgments.
Summarize the following judgment in exactly 2-3 sentences. Do not use formatting like bullet points, markdown headers, or intro phrases. Provide a clean, single paragraph.

Case: {title}
Judgment Snippet:
{snippet}
"""
            try:
                response = client.chat.completions.create(
                    model="llama-3.1-8b-instant",
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=150,
                    temperature=0.3
                )
                summary = response.choices[0].message.content.strip()
                
                # Save to database
                cur.execute("UPDATE cases SET summary = %s WHERE id = %s", (summary, cid))
                conn.commit()
                success_count += 1
                print(f"  Success. Summary: {summary[:100]}...")
                
            except Exception as e:
                conn.rollback()
                print(f"  Failed to summarize case {cid}: {e}")
                # Wait longer if we hit a rate limit
                if "rate limit" in str(e).lower():
                    print("  Rate limit hit. Sleeping for 15 seconds...")
                    time.sleep(15)
            
            # Throttle requests
            time.sleep(args.delay)

        print(f"Summarization run completed. Successfully summarized {success_count} / {len(rows)} cases.")

    finally:
        cur.close()
        conn.close()

if __name__ == "__main__":
    main()
