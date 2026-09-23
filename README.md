---
title: Nyay Khoj
emoji: ⚖️
colorFrom: yellow
colorTo: blue
sdk: docker
app_port: 7860
---

# Nyay Khoj Backend

Indian Legal Search API backend — Flask + PostgreSQL/pgvector + hybrid BM25/vector search + Legal-BERT classifiers + Groq LLaMA summaries.

## Tech Stack
- **Framework:** Flask, gunicorn
- **Database & Search:** PostgreSQL with pgvector for dense similarity and BM25
- **AI/ML:** sentence-transformers for embeddings, Groq API for summaries

## Dataset
22,904 Supreme Court/High Court judgments, sourced from Indian Kanoon.

## How to Run Locally
1. Create and activate a virtual environment (e.g., `python3 -m venv venv && source venv/bin/activate`).
2. Set up your `.env` file with `DATABASE_URL` and `GROQ_API_KEY`.
3. Install dependencies: `pip install -r requirements.txt`
4. Run the server: `python app.py`
