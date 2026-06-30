import requests
import psycopg2
import time
import os
import re
import argparse
from bs4 import BeautifulSoup

CATEGORIES = {
    "Industrial&Labour": "industrial disputes act OR labour act OR workmen compensation OR factories act OR trade union act",
    "Tax": "income tax act OR tax act OR gst OR customs act OR service tax",
    "Financial": "banking regulation act OR rbi act OR securities act OR insurance act OR nbfc",
    "Civil": "civil procedure code OR cpc OR contract act OR specific relief act OR limitation act",
    "Constitution": "article 14 OR article 21 OR fundamental rights OR writ petition OR constitutional validity",
    "Criminal": "indian penal code OR ipc OR murder OR theft OR fraud OR criminal procedure",
    "Land&Property": "land acquisition act OR transfer of property act OR rent control OR tenancy act",
    "Motorvehicles": "motor vehicles act OR mv act OR accident compensation OR tribunal award",
}

TARGET = 3000

def get_existing_titles(conn):
    cur = conn.cursor()
    cur.execute("SELECT title FROM cases")
    titles = {row[0] for row in cur.fetchall()}
    cur.close()
    print(f"Loaded {len(titles)} existing titles")
    return titles

def get_counts(conn):
    cur = conn.cursor()
    cur.execute("SELECT case_type, COUNT(*) FROM cases GROUP BY case_type")
    counts = {row[0]: row[1] for row in cur.fetchall()}
    cur.close()
    return counts

def extract_verdict(text):
    t = text.lower()
    if "appeal allowed" in t or "petition allowed" in t:
        return "Appeal Allowed"
    elif "acquitted" in t:
        return "Acquitted"
    elif "convicted" in t:
        return "Convicted"
    elif "dismissed" in t:
        return "Dismissed"
    return "Dismissed"

def extract_ipc(text):
    sections = re.findall(r'[Ss]ection\s+(\d+[A-Za-z]*)\s+(?:of\s+)?(?:IPC|I\.P\.C|Indian Penal Code)', text)
    return ", ".join(set(sections[:5])) if sections else None

def scrape_category(category, query, needed, existing_titles, conn):
    print(f"Category: {category} | Need: {needed} more cases")
    headers = {"User-Agent": "Mozilla/5.0 (research bot)"}
    scraped = 0
    page = 0

    while scraped < needed:
        try:
            resp = requests.get(
                "https://indiankanoon.org/search/",
                params={"formInput": f"{query} doctypes:supremecourt", "pagenum": page},
                headers=headers, timeout=15
            )
            if resp.status_code != 200:
                print(f"  HTTP {resp.status_code}, stopping")
                break

            soup = BeautifulSoup(resp.text, "html.parser")
            # New IK structure: case links are /doc/XXXXXX/
            all_links = soup.find_all("a", href=True)
            results = [a for a in all_links if "/doc/" in a.get("href", "")]
            if not results:
                print(f"  No more results at page {page}")
                break

            for link in results:
                if scraped >= needed:
                    break
                href = link.get("href", "")
                case_url = f"https://indiankanoon.org{href}"
                # Get title from the case page itself
                title = href  # temp, will be replaced below
                if href in existing_titles:
                    continue
                time.sleep(1.5)
                try:
                    case_resp = requests.get(case_url, headers=headers, timeout=15)
                    case_soup = BeautifulSoup(case_resp.text, "html.parser")
                    judgment_div = case_soup.find("div", id="maindoc")
                    if not judgment_div:
                        continue
                    full_text = judgment_div.get_text(separator=" ", strip=True)
                    if len(full_text) < 200:
                        continue
                    full_text = full_text[:10000]
                    # Get actual title from case page
                    title_tag = case_soup.find("title")
                    title = title_tag.get_text(strip=True) if title_tag else href
                    title = title.replace(" | Indian Kanoon", "").strip()

                    if title in existing_titles:
                        continue

                    docsource = case_soup.find("span", class_="docsource_main")
                    court_name = docsource.get_text(strip=True) if docsource else "Supreme Court of India"
                    verdict = extract_verdict(full_text)
                    ipc_sections = extract_ipc(full_text) if category == "Criminal" else None
                    cur = conn.cursor()
                    cur.execute("""
                        INSERT INTO cases (title, text, case_type, verdict, court_name, court_type, doc_url, ipc_sections)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                    """, (title, full_text, category, verdict, court_name, "Supreme Court", case_url, ipc_sections))
                    conn.commit()
                    cur.close()
                    existing_titles.add(title)
                    scraped += 1
                    print(f"  [{scraped}/{needed}] {title[:70]}")
                except Exception as e:
                    print(f"  Case error: {e}")
                    try: conn.rollback()
                    except: pass

            page += 1
            time.sleep(2)
        except Exception as e:
            print(f"  Page error: {e}")
            time.sleep(5)
            break

    print(f"  Finished {category}: {scraped} added")
    return scraped

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--category", type=str, default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    db_url = os.environ.get("DATABASE_URL", "postgresql://localhost/legaldb")
    conn = psycopg2.connect(db_url)
    existing_titles = get_existing_titles(conn)
    counts = get_counts(conn)
    print("Current distribution:", counts)
    total = 0
    for category, query in CATEGORIES.items():
        if args.category and category != args.category:
            continue
        current = counts.get(category, 0)
        needed = args.limit if args.limit else max(0, TARGET - current)
        if needed == 0:
            print(f"{category}: already at target, skipping")
            continue
        total += scrape_category(category, query, needed, existing_titles, conn)
        time.sleep(5)
    conn.close()
    print(f"Done! Total new cases added: {total}")

if __name__ == "__main__":
    main()
