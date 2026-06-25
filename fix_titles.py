"""
fix_titles.py — Fix garbled case titles in the DB.

Two-pass strategy:
  PASS 1: Strip HTML tags from titles that have them (the title text itself is fine, just wrapped in <b> tags)
  PASS 2: Re-parse from stored text for titles that are genuine garbage (-- ;, etc.)
"""

import re
import psycopg2

DB_URL = "postgresql://localhost/legaldb"

# ── Pattern sets ──────────────────────────────────────────────────────────────

HTML_TAG_RE = re.compile(r"<[^>]+>")
SOFT_HYPHEN_RE = re.compile(r"\u00ad")

# Titles that have HTML but whose text content is actually fine
def has_html_tags(title: str) -> bool:
    return bool(HTML_TAG_RE.search(title))

def strip_html(title: str) -> str:
    clean = HTML_TAG_RE.sub("", title)
    clean = SOFT_HYPHEN_RE.sub("", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:300]

# Titles that are genuine garbage (PDF artifacts, template boilerplate)
GENUINE_GARBAGE_RE = [
    re.compile(r"^[-;.,~'|\\/*=#+@!%^&(){}\[\]<>\u00ad\s]{0,8}$"),  # all punctuation
    re.compile(r"^[-;.,\u00ad]{1,5}\s"),                              # starts with -- or ;
    re.compile(r"^\d+\s+Whether\s+This\s+Case", re.I),               # Gujarat HC template
    re.compile(r"^\d+\s+(Whether\s+It\s+Is|Circulate\s+To)", re.I),
    re.compile(r"^\(O&M;?\)"),                                        # Punjab/Haryana format glitch
]

def is_genuine_garbage(title: str) -> bool:
    for pat in GENUINE_GARBAGE_RE:
        if pat.match(title):
            return True
    return False

# ── Text extraction from stored judgment text ─────────────────────────────────

SKIP_LINE_RE = [
    re.compile(r"^\[?\d{4}\]\s+\d+\s+S\.?C\.?R"),   # [2013] 17 S.C.R. 361
    re.compile(r"^[-;.,~\"'|\\/*=#+@!%^&()]{1,15}$"),  # pure punctuation
    re.compile(r"^\d+\s*$"),                           # just a number
    re.compile(r"^\d+\s+Whether\s+This\s+Case", re.I),
    re.compile(r"^\d+\s+(Whether\s+It\s+Is|Circulate\s+To)", re.I),
    re.compile(r"^(IN THE (HIGH|SUPREME) COURT)", re.I),
    re.compile(r"^(BEFORE THE\s)", re.I),
    re.compile(r"^(DATED?:?\s)", re.I),
    re.compile(r"^(CORAM:?\s)", re.I),
    re.compile(r"^JUDGMENT\s*$", re.I),
    re.compile(r"^ORDER\s*$", re.I),
    re.compile(r"^(FAO|SCA|CR\.MA|SCR\.A|OJCA|OCA|C/SCA|R/CR)\s*/?\s*No\.", re.I),
    re.compile(r"^\(O&M;?\)"),
]

STOP_LINE_RE = [
    re.compile(r"^\s*(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+\d", re.I),
    re.compile(r"^\[.*JJ\.?\]"),
    re.compile(r"^\d{4}\s*[-—]"),
    re.compile(r"^(JUDGMENT|ORDER)\s+\w+,\s+J\.?", re.I),  # "JUDGMENT Mohan, J"
    re.compile(r"^(CORAM|THE HON)", re.I),
]


def should_skip(line: str) -> bool:
    line_clean = HTML_TAG_RE.sub("", line).strip()
    for pat in SKIP_LINE_RE:
        if pat.match(line_clean):
            return True
    return False


def should_stop(line: str) -> bool:
    for pat in STOP_LINE_RE:
        if pat.match(line.strip()):
            return True
    return False


def extract_title_from_text(text: str, fallback: str) -> str:
    """Extract clean case name from judgment text (for genuine garbage titles)."""
    text = HTML_TAG_RE.sub(" ", text)
    text = SOFT_HYPHEN_RE.sub("", text)
    text = re.sub(r"&amp;", "&", text)

    lines = [l.strip() for l in text.split("\n") if l.strip()]
    collected = []
    found_start = False

    for line in lines[:100]:
        if not found_start:
            if should_skip(line):
                continue
            if re.search(r"[A-Za-z]{3,}", line):
                found_start = True
            else:
                continue

        if should_stop(line):
            break
        if should_skip(line):
            break
        if len(line) > 150:
            break

        collected.append(line)
        if len(collected) >= 3:
            break

    if not collected:
        return fallback

    title = " ".join(collected)
    title = re.sub(r"\s+", " ", title).strip(" .,;:-")
    if not title or not re.search(r"[A-Za-z]{2,}", title):
        return fallback
    return title[:300]


# ── Main ──────────────────────────────────────────────────────────────────────

def fix_garbled_titles(dry_run=False, limit=None):
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()

    cur.execute("SELECT id, title, left(text, 6000) FROM cases ORDER BY id")
    rows = cur.fetchall()
    print(f"Total cases: {len(rows)}")

    html_cases = [(rid, t, txt) for rid, t, txt in rows if has_html_tags(t) and not is_genuine_garbage(t)]
    garbage_cases = [(rid, t, txt) for rid, t, txt in rows if is_genuine_garbage(t)]

    print(f"  HTML-tagged titles (strip only): {len(html_cases)}")
    print(f"  Genuine garbage titles (re-parse): {len(garbage_cases)}")

    if limit:
        html_cases = html_cases[:limit // 2]
        garbage_cases = garbage_cases[:limit // 2]

    html_fixed = 0
    garbage_fixed = 0
    skipped = 0

    # PASS 1: Strip HTML from titles
    for row_id, old_title, _ in html_cases:
        new_title = strip_html(old_title)
        if new_title == old_title or not new_title:
            skipped += 1
            continue
        if dry_run:
            print(f"  HTML [{row_id}] '{old_title[:50]}' → '{new_title[:70]}'")
        else:
            cur.execute("UPDATE cases SET title = %s WHERE id = %s", (new_title, row_id))
            html_fixed += 1
            if html_fixed % 200 == 0:
                conn.commit()
                print(f"  HTML fixed: {html_fixed}/{len(html_cases)}")

    if not dry_run and html_fixed > 0:
        conn.commit()
        print(f"Pass 1 done: {html_fixed} HTML titles cleaned")

    # PASS 2: Re-parse genuine garbage from text
    for row_id, old_title, text in garbage_cases:
        new_title = extract_title_from_text(text, old_title)
        if new_title == old_title or not new_title or is_genuine_garbage(new_title):
            skipped += 1
            if dry_run:
                print(f"  SKIP [{row_id}] '{old_title[:60]}'")
            continue
        if dry_run:
            print(f"  PARSE [{row_id}] '{old_title[:40]}' → '{new_title[:80]}'")
        else:
            cur.execute("UPDATE cases SET title = %s WHERE id = %s", (new_title, row_id))
            garbage_fixed += 1
            if garbage_fixed % 200 == 0:
                conn.commit()
                print(f"  Garbage fixed: {garbage_fixed}/{len(garbage_cases)}")

    if not dry_run:
        conn.commit()
        total = html_fixed + garbage_fixed
        print(f"\nAll done! HTML stripped: {html_fixed} | Garbage re-parsed: {garbage_fixed} | Skipped: {skipped} | Total fixed: {total}")
    else:
        print(f"\nDry-run complete.")

    cur.close()
    conn.close()


if __name__ == "__main__":
    import sys
    dry = "--dry-run" in sys.argv
    lim = None
    for arg in sys.argv[1:]:
        if arg.startswith("--limit="):
            lim = int(arg.split("=")[1])
    if dry:
        print("=== DRY RUN MODE ===")
    fix_garbled_titles(dry_run=dry, limit=lim)
