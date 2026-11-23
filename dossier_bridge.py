"""
dossier_bridge.py — PM Career Copilot: Unified Multi-Source Job Ingestion & Execution Bridge
=============================================================================================
Sprint 3: HITL Multi-Source Pipeline Orchestrator.

Ingestion Channels:
  1. Direct JSearch India Poller (active when RAPIDAPI_KEY is configured in .env):
     - Hits https://jsearch.p.rapidapi.com/search (country="in", date_posted="3days", num_pages="1")
     - Runs 3 quota-safe Boolean queries:
       * ("Associate Product Manager" OR "Product Manager" OR "Product Owner" OR "Digital Product") India
       * ("Product Strategy" OR "Senior Product Analyst" OR "Product Analyst" OR "Founder's Office") India
       * ("Product Marketing Manager" OR "Product Growth") India
  2. dossier_crm Reader:
     - Scans ./dossier_crm SQLite databases and JSON export files for new postings.
  3. ./manual_jds/ Folder Watcher:
     - Scans .txt and .md job descriptions placed in ./manual_jds/.
     - Extracts company, role, location, and apply URL via Gemini when unstructured.

Pipeline Gate & Deduplication:
  - job_hash = sha256((company.lower() + role.lower()).encode("utf-8")).hexdigest()
  - Skips already-processed jobs recorded in copilot_state.db.
  - Triage via triage_and_build.py (Rule 5 updated rubric).
  - If fit_score >= 75: compiles Resume_<Company>_<Role>.pdf and triggers outreach_drafter.py.
  - Persists full execution audit trail into copilot_state.db.

Usage:
  python dossier_bridge.py [--dry-run] [--force] [--manual-only]
  python dossier_bridge.py --stats
  python dossier_bridge.py --jd-file manual_jds/sample_jd.txt --company "Acme Analytics" --role "Product Manager"
"""

import os
import sys
import re
import json
import sqlite3
import hashlib
import argparse
import urllib.parse
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

# Root and configuration
ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

STATE_DB_PATH    = ROOT / "copilot_state.db"
MANUAL_JDS_DIR   = ROOT / "manual_jds"
DOSSIER_CRM_DIR  = ROOT / "dossier_crm"

# Import Pipeline Modules
try:
    from triage_and_build import build_resume_for_jd, FIT_SCORE_THRESHOLD
except ImportError as e:
    print(f"[bridge] ⚠️ Warning: Failed to import triage_and_build: {e}")
    build_resume_for_jd = None
    FIT_SCORE_THRESHOLD = 75

try:
    from outreach_drafter import stage_outreach, get_credentials_path, call_gemini
except ImportError as e:
    print(f"[bridge] ⚠️ Warning: Failed to import outreach_drafter: {e}")
    stage_outreach = None
    get_credentials_path = None
    call_gemini = None

import requests


# ─────────────────────────────────────────────────────────────────────────────
# 1. STATE DATABASE (copilot_state.db)
# ─────────────────────────────────────────────────────────────────────────────

def init_state_db(db_path: Path = STATE_DB_PATH) -> sqlite3.Connection:
    """Initializes the SQLite state tracking database with the 15-column schema."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_jobs (
                job_hash TEXT PRIMARY KEY,
                source TEXT,
                company TEXT,
                role TEXT,
                location TEXT,
                url TEXT,
                date_posted TEXT,
                fit_score INTEGER,
                pdf_path TEXT,
                contact_name TEXT,
                contact_title TEXT,
                contact_email TEXT,
                alum_tag TEXT,
                draft_status TEXT,
                created_at TEXT
            );
        """)
        # Backward-compatibility column migration if table already existed with fewer columns
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(processed_jobs)")
        existing_cols = {row["name"] for row in cur.fetchall()}
        required_cols = {
            "source": "TEXT",
            "location": "TEXT",
            "date_posted": "TEXT",
            "contact_title": "TEXT"
        }
        for col_name, col_type in required_cols.items():
            if col_name not in existing_cols:
                conn.execute(f"ALTER TABLE processed_jobs ADD COLUMN {col_name} {col_type};")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_processed_company ON processed_jobs(company);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_processed_status ON processed_jobs(draft_status);")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS parsed_emails (
                message_id TEXT PRIMARY KEY,
                sender TEXT,
                subject TEXT,
                date_received TEXT,
                job_count INTEGER,
                parsed_at TEXT
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_parsed_emails_date ON parsed_emails(date_received);")
    return conn


def compute_job_hash(company: str, role: str) -> str:
    """Computes a deterministic SHA-256 fingerprint from company.lower() + role.lower()."""
    clean_str = company.strip().lower() + role.strip().lower()
    return hashlib.sha256(clean_str.encode("utf-8")).hexdigest()


def is_job_processed(job_hash: str, db_path: Path = STATE_DB_PATH) -> bool:
    """Checks if a job hash has already been recorded in copilot_state.db."""
    conn = init_state_db(db_path)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM processed_jobs WHERE job_hash = ?", (job_hash,))
    row = cur.fetchone()
    conn.close()
    return row is not None


def get_processed_job(job_hash: str, db_path: Path = STATE_DB_PATH) -> dict | None:
    """Fetches a processed job record by hash."""
    conn = init_state_db(db_path)
    cur = conn.cursor()
    cur.execute("SELECT * FROM processed_jobs WHERE job_hash = ?", (job_hash,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def record_processed_job(record: dict, db_path: Path = STATE_DB_PATH):
    """Saves or updates a processed job record with all 15 columns in copilot_state.db."""
    conn = init_state_db(db_path)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with conn:
        conn.execute("""
            INSERT INTO processed_jobs (
                job_hash, source, company, role, location, url, date_posted,
                fit_score, pdf_path, contact_name, contact_title, contact_email,
                alum_tag, draft_status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_hash) DO UPDATE SET
                source = excluded.source,
                company = excluded.company,
                role = excluded.role,
                location = excluded.location,
                url = excluded.url,
                date_posted = excluded.date_posted,
                fit_score = excluded.fit_score,
                pdf_path = excluded.pdf_path,
                contact_name = excluded.contact_name,
                contact_title = excluded.contact_title,
                contact_email = excluded.contact_email,
                alum_tag = excluded.alum_tag,
                draft_status = excluded.draft_status,
                created_at = excluded.created_at;
        """, (
            record.get("job_hash", ""),
            record.get("source", "manual"),
            record.get("company", ""),
            record.get("role", ""),
            record.get("location", ""),
            record.get("url", ""),
            record.get("date_posted", ""),
            record.get("fit_score", 0),
            record.get("pdf_path", ""),
            record.get("contact_name", ""),
            record.get("contact_title", ""),
            record.get("contact_email", ""),
            record.get("alum_tag", "None"),
            record.get("draft_status", ""),
            now_str
        ))
    conn.close()


def print_state_summary(db_path: Path = STATE_DB_PATH):
    """Prints a formatted summary table of all processed jobs."""
    if not db_path.exists():
        print(f"[db] State database {db_path.name} does not exist yet.")
        return

    conn = init_state_db(db_path)
    cur = conn.cursor()
    cur.execute("SELECT * FROM processed_jobs ORDER BY created_at DESC")
    rows = cur.fetchall()
    conn.close()

    print(f"\n{'='*105}")
    print(f"  📊  PM CAREER COPILOT — PROCESSED JOBS AUDIT TRAIL ({len(rows)} Records)")
    print(f"{'='*105}")
    print(f"{'Company':<20} {'Role':<24} {'Score':<6} {'Contact':<18} {'Alum':<10} {'Status':<16} {'Source':<8}")
    print(f"{'-'*20} {'-'*24} {'-'*6} {'-'*18} {'-'*10} {'-'*16} {'-'*8}")
    for r in rows:
        co = (r['company'][:18] + '..') if len(r['company']) > 18 else r['company']
        ro = (r['role'][:22] + '..') if len(r['role']) > 22 else r['role']
        score = f"{r['fit_score']}/100"
        contact = (r['contact_name'][:16] + '..') if r['contact_name'] and len(r['contact_name']) > 16 else (r['contact_name'] or "—")
        alum = r['alum_tag'] or "None"
        status = r['draft_status'] or "—"
        src = (r['source'][:7]) if r['source'] else "manual"
        print(f"{co:<20} {ro:<24} {score:<6} {contact:<18} {alum:<10} {status:<16} {src:<8}")
    print(f"{'='*105}\n")


# ─────────────────────────────────────────────────────────────────────────────
# 2. INGESTION CHANNELS
# ─────────────────────────────────────────────────────────────────────────────

# Channel 1: Direct JSearch India Poller
JSEARCH_QUERIES = [
    '("Associate Product Manager" OR "Product Manager" OR "Digital Product Manager" OR "Product Owner") India',
    '("Technical Product Manager" OR "AI Product Manager" OR "Data Product Manager") India',
    '("Product Strategy Manager" OR "Product Operations Manager" OR "Product Strategy" OR "Founder\'s Office") India',
    '("Growth Product Manager" OR "Product Marketing Manager" OR "Product Growth") India'
]

def poll_jsearch_india() -> list[dict]:
    """
    Direct JSearch India Poller:
    Queries https://jsearch.p.rapidapi.com/search with country="in", date_posted="3days", num_pages="1"
    across the 4 quota-safe Boolean query tracks.
    """
    rapidapi_key = os.environ.get("RAPIDAPI_KEY", "").strip()
    if not rapidapi_key:
        print("[jsearch] ℹ️  RAPIDAPI_KEY not found in .env; skipping direct JSearch poller.")
        return []

    url = "https://jsearch.p.rapidapi.com/search"
    headers = {
        "X-RapidAPI-Key": rapidapi_key,
        "X-RapidAPI-Host": "jsearch.p.rapidapi.com"
    }

    jobs = []
    print(f"[jsearch] 🔍 Polling JSearch India across {len(JSEARCH_QUERIES)} target queries...")
    for idx, q in enumerate(JSEARCH_QUERIES, 1):
        params = {
            "query": q,
            "country": "in",
            "date_posted": "3days",
            "num_pages": "1"
        }
        try:
            # Try search-v2 endpoint first (RapidAPI standard) with fallback to /search
            resp = requests.get("https://jsearch.p.rapidapi.com/search-v2", headers=headers, params=params, timeout=15)
            if resp.status_code != 200:
                resp = requests.get("https://jsearch.p.rapidapi.com/search", headers=headers, params=params, timeout=15)

            if resp.status_code == 200:
                res_json = resp.json()
                raw_data = res_json.get("data", [])
                if isinstance(raw_data, dict):
                    data = raw_data.get("jobs", [])
                elif isinstance(raw_data, list):
                    data = raw_data
                else:
                    data = []
                print(f"[jsearch]   Query {idx}/{len(JSEARCH_QUERIES)} fetched {len(data)} postings: '{q[:40]}...'")
                for item in data:
                    co = (item.get("employer_name") or "TargetCompany").strip()
                    ro = (item.get("job_title") or "Product Manager").strip()
                    loc = f"{item.get('job_city') or ''}, {item.get('job_country') or 'India'}".strip(", ")
                    apply_url = item.get("job_apply_link") or item.get("job_google_link") or ""
                    posted = item.get("job_posted_at_datetime_utc") or ""
                    desc = item.get("job_description") or ""
                    if desc:
                        jobs.append({
                            "source": "jsearch",
                            "company": co,
                            "role": ro,
                            "location": loc or "India",
                            "url": apply_url,
                            "date_posted": posted,
                            "jd_text": desc
                        })
            else:
                print(f"[jsearch]   Query {idx} failed (status {resp.status_code}): {resp.text[:100]}")
        except Exception as e:
            print(f"[jsearch]   Query {idx} request exception: {e}")

    return jobs


# Channel 2: dossier_crm Reader
def read_dossier_crm_jobs() -> list[dict]:
    """Inspects ./dossier_crm for SQLite or JSON job records saved in the local Dossier CRM."""
    candidate_dbs = [
        DOSSIER_CRM_DIR / "prisma" / "dev.db",
        DOSSIER_CRM_DIR / "dossier.db",
        DOSSIER_CRM_DIR / "data" / "dossier.db",
        ROOT / "dossier.db"
    ]

    custom_url = os.environ.get("DATABASE_URL", "")
    if custom_url.startswith("file:"):
        candidate_dbs.insert(0, Path(custom_url.replace("file:", "")))

    for db_path in candidate_dbs:
        if db_path.exists():
            try:
                conn = sqlite3.connect(str(db_path))
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='Posting'")
                if cur.fetchone():
                    cur.execute("""
                        SELECT id, company, title, place, url, description, postedAt
                        FROM Posting
                        WHERE dismissedAt IS NULL
                        ORDER BY createdAt DESC
                    """)
                    rows = cur.fetchall()
                    conn.close()
                    dossier_jobs = []
                    for r in rows:
                        dossier_jobs.append({
                            "source": "dossier_crm",
                            "company": r["company"] or "TargetCompany",
                            "role": r["title"] or "Product Manager",
                            "location": r["place"] or "India",
                            "url": r["url"] or "",
                            "date_posted": str(r["postedAt"]) if r["postedAt"] else "",
                            "jd_text": r["description"] or ""
                        })
                    return dossier_jobs
                conn.close()
            except Exception as e:
                print(f"[dossier] Notice checking {db_path.name}: {e}")

    return []


# Channel 3: ./manual_jds/ Folder Watcher
def extract_metadata_with_gemini(raw_text: str, filename: str = "") -> dict:
    """Uses Gemini (with regex fallback) to extract company, role, location, and apply URL."""
    if call_gemini:
        prompt = f"""
You are an expert HR data extractor. Extract the metadata from the following Job Description (JD).
Filename: {filename}

JD Text:
\"\"\"{raw_text[:2500]}\"\"\"

Return ONLY valid JSON with this schema:
{{
  "company": "Company Name",
  "role": "Role Title",
  "location": "City, Country (or Remote)",
  "url": "Application URL if present or empty string"
}}
"""
        resp_text = call_gemini(prompt)
        if resp_text:
            try:
                cleaned = re.sub(r"^```json\s*", "", resp_text.strip(), flags=re.MULTILINE)
                cleaned = re.sub(r"```$", "", cleaned.strip(), flags=re.MULTILINE)
                data = json.loads(cleaned)
                if data and data.get("company") and data.get("role"):
                    return data
            except Exception:
                pass

    # Heuristic & Regex Fallback
    lines = raw_text.strip().splitlines()
    company = ""
    role = ""
    location = "India"
    url = ""
    for l in lines[:15]:
        if re.match(r"^company\s*:\s*", l, re.I):
            company = re.sub(r"^company\s*:\s*", "", l, flags=re.I).strip()
        elif re.match(r"^(role|title)\s*:\s*", l, re.I):
            role = re.sub(r"^(role|title)\s*:\s*", "", l, flags=re.I).strip()
        elif re.match(r"^location\s*:\s*", l, re.I):
            location = re.sub(r"^location\s*:\s*", "", l, flags=re.I).strip()
        elif re.match(r"^(url|link)\s*:\s*", l, re.I):
            url = re.sub(r"^(url|link)\s*:\s*", "", l, flags=re.I).strip()

    if (not company or not role) and filename:
        stem = Path(filename).stem
        if "_" in stem:
            parts = stem.split("_", 1)
            company = company or parts[0].replace("-", " ")
            role = role or parts[1].replace("-", " ")
        elif not company:
            company = stem.replace("-", " ")

    if not company:
        m = re.search(r"About\s+([A-Z][A-Za-z0-9\s&]+?):", raw_text)
        company = m.group(1).strip() if m else "TargetCompany"
    if not role:
        role = "Product Manager"

    return {"company": company, "role": role, "location": location, "url": url}


def read_manual_jds() -> list[dict]:
    """Reads all .txt and .md files in ./manual_jds/."""
    jobs = []
    if not MANUAL_JDS_DIR.exists():
        MANUAL_JDS_DIR.mkdir(parents=True, exist_ok=True)
        return jobs

    for file_path in sorted(MANUAL_JDS_DIR.glob("*.*")):
        if file_path.name.startswith("."):
            continue
        if file_path.suffix.lower() in [".txt", ".md"]:
            try:
                content = file_path.read_text(encoding="utf-8")
                meta = extract_metadata_with_gemini(content, file_path.name)
                jobs.append({
                    "source": f"manual:{file_path.name}",
                    "company": meta.get("company", "TargetCompany"),
                    "role": meta.get("role", "Product Manager"),
                    "location": meta.get("location", "India"),
                    "url": meta.get("url", ""),
                    "date_posted": datetime.now().strftime("%Y-%m-%d"),
                    "jd_text": content,
                    "file_path": str(file_path)
                })
            except Exception as e:
                print(f"[manual] Error reading {file_path.name}: {e}")

    return jobs


# ─────────────────────────────────────────────────────────────────────────────
# 3. PIPELINE ORCHESTRATION & GATE EXECUTION
# ─────────────────────────────────────────────────────────────────────────────

def process_job(job: dict, dry_run: bool = False, force: bool = False) -> dict:
    """
    Processes a single job candidate:
      1. Computes job_hash = sha256(company.lower() + role.lower()).
      2. Checks copilot_state.db deduplication.
      3. Evaluates fit_score via triage_and_build.py (Rule 5 rubric).
      4. If fit_score >= 75: compiles Resume_<Company>_<Role>.pdf and triggers outreach_drafter.py.
      5. Records 15-column audit record in copilot_state.db.
    """
    company = (job.get("company") or "TargetCompany").strip()
    role    = (job.get("role") or "Product Manager").strip()
    location = (job.get("location") or "India").strip()
    url     = (job.get("url") or "").strip()
    posted  = (job.get("date_posted") or "").strip()
    jd_text = (job.get("jd_text") or "").strip()
    source  = (job.get("source") or "manual").strip()

    if not jd_text:
        print(f"[bridge] ⚠️ Job {company} — {role} has empty JD text. Skipping.")
        return {"status": "EMPTY_JD", "company": company, "role": role}

    # 1. Deduplication Check
    job_hash = compute_job_hash(company, role)
    if not force and is_job_processed(job_hash):
        existing = get_processed_job(job_hash)
        status = existing.get("draft_status", "PROCESSED") if existing else "PROCESSED"
        print(f"[skip] ⏭️  Already processed: {company} — {role} (Hash: {job_hash[:10]}.. | Status: {status})")
        return {"status": "SKIPPED", "job_hash": job_hash, "company": company, "role": role}

    print(f"\n{'='*75}")
    print(f"  🚀  PIPELINE ENGAGED: {company} — {role}")
    print(f"      Source: {source} | Location: {location} | Hash: {job_hash[:10]}...")
    print(f"{'='*75}")

    # 2. Fit Evaluation & Resume Build
    if not build_resume_for_jd:
        print("[bridge] ❌ Error: triage_and_build module unavailable.")
        return {"status": "FAILED", "company": company, "role": role}

    triage_res = build_resume_for_jd(jd_text, company_name=company, role_title=role)
    fit_score = triage_res.get("fit_score", 0)
    verdict   = triage_res.get("verdict", "LOW_FIT")
    pdf_path  = triage_res.get("pdf_path")
    bullets   = triage_res.get("selected_bullets")

    # Gate: Fit Score Check
    if fit_score < FIT_SCORE_THRESHOLD or verdict == "REJECT":
        print(f"\n[gate] ❌ REJECT: Fit score {fit_score} < threshold {FIT_SCORE_THRESHOLD} ({verdict}).")
        print(f"       Skipping resume compilation and outreach staging.")
        record = {
            "job_hash": job_hash,
            "source": source,
            "company": company,
            "role": role,
            "location": location,
            "url": url,
            "date_posted": posted,
            "fit_score": fit_score,
            "pdf_path": "",
            "contact_name": "",
            "contact_title": "",
            "contact_email": "",
            "alum_tag": "None",
            "draft_status": "REJECTED"
        }
        record_processed_job(record)
        return {"status": "REJECTED", "fit_score": fit_score, "company": company, "role": role}

    print(f"\n[gate] ✅ ACCEPT: Fit score {fit_score} >= {FIT_SCORE_THRESHOLD} ({verdict})")
    print(f"       Bespoke 1-Page PDF generated: {pdf_path.name if pdf_path else 'None'}")

    # Extract top proof point for outreach
    top_bullet = ""
    if bullets and bullets.get("iqvia") and len(bullets["iqvia"]) > 0:
        top_bullet = bullets["iqvia"][0]

    # 3. Outreach Contact Discovery & Draft Staging
    if not stage_outreach:
        print("[bridge] ⚠️ Warning: outreach_drafter module unavailable.")
        outreach_res = {
            "contact_name": "Product Team",
            "contact_title": "Hiring Team",
            "contact_email": f"hiring@{re.sub(r'[^\w]', '', company.lower())}.com",
            "alum_tag": "None",
            "draft_status": "OUTREACH_MODULE_MISSING"
        }
    else:
        outreach_res = stage_outreach(
            company=company,
            role=role,
            jd_text=jd_text,
            pdf_path=pdf_path,
            top_bullet=top_bullet,
            dry_run=dry_run
        )

    # 4. Save 15-column record in copilot_state.db
    record = {
        "job_hash": job_hash,
        "source": source,
        "company": company,
        "role": role,
        "location": location,
        "url": url,
        "date_posted": posted,
        "fit_score": fit_score,
        "pdf_path": str(pdf_path) if pdf_path else "",
        "contact_name": outreach_res.get("contact_name", ""),
        "contact_title": outreach_res.get("contact_title", ""),
        "contact_email": outreach_res.get("contact_email", ""),
        "alum_tag": outreach_res.get("alum_tag", "None"),
        "draft_status": outreach_res.get("draft_status", "PROCESSED")
    }
    record_processed_job(record)
    print(f"\n[state] 💾 Recorded in copilot_state.db with status: {record['draft_status']}")

    return {
        "status": "COMPLETED",
        "job_hash": job_hash,
        "company": company,
        "role": role,
        "fit_score": fit_score,
        "pdf_path": str(pdf_path) if pdf_path else "",
        "contact_email": outreach_res.get("contact_email", ""),
        "draft_status": record["draft_status"]
    }


def run_bridge(dry_run: bool = False, manual_only: bool = False, force: bool = False):
    """Orchestrates ingestion across JSearch, dossier_crm, and manual_jds/."""
    init_state_db()

    all_jobs = []

    # Channel 1: JSearch India Poller
    if not manual_only:
        jsearch_jobs = poll_jsearch_india()
        all_jobs.extend(jsearch_jobs)

    # Channel 2: dossier_crm Reader
    if not manual_only:
        dossier_jobs = read_dossier_crm_jobs()
        if dossier_jobs:
            print(f"[ingest] Discovered {len(dossier_jobs)} jobs from Dossier CRM.")
            all_jobs.extend(dossier_jobs)

    # Channel 3: ./manual_jds/ Folder Watcher
    manual_jobs = read_manual_jds()
    print(f"[ingest] Discovered {len(manual_jobs)} jobs in {MANUAL_JDS_DIR.name}/ folder.")
    all_jobs.extend(manual_jobs)

    if not all_jobs:
        print(f"\n[bridge] ℹ️  No jobs found to process. Drop .txt or .md files in {MANUAL_JDS_DIR.name}/ or sync JSearch/Dossier.")
        return

    print(f"\n[bridge] Total candidate jobs queued across channels: {len(all_jobs)}")

    processed_count = 0
    skipped_count = 0
    accepted_count = 0
    rejected_count = 0

    for job in all_jobs:
        res = process_job(job, dry_run=dry_run, force=force)
        st = res.get("status")
        if st == "SKIPPED":
            skipped_count += 1
        elif st == "COMPLETED":
            processed_count += 1
            accepted_count += 1
        elif st == "REJECTED":
            processed_count += 1
            rejected_count += 1

    print(f"\n{'='*75}")
    print(f"  🏁  BATCH COMPLETE: {len(all_jobs)} Evaluated")
    print(f"      Accepted & Staged : {accepted_count}")
    print(f"      Rejected (< 75)   : {rejected_count}")
    print(f"      Skipped (Duplicate): {skipped_count}")
    print(f"{'='*75}\n")

    print_state_summary()


# ─────────────────────────────────────────────────────────────────────────────
# 4. CLI INTERFACE
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="PM Career Copilot — Unified Multi-Source Job Ingestion & Bridge")
    parser.add_argument("--dry-run", action="store_true", help="Run without creating live Gmail drafts")
    parser.add_argument("--force", action="store_true", help="Re-process jobs even if already in state DB")
    parser.add_argument("--manual-only", action="store_true", help="Scan only ./manual_jds/ and ignore JSearch/Dossier")
    parser.add_argument("--stats", action="store_true", help="Print summary table of processed jobs and exit")
    parser.add_argument("--jd-file", default="", help="Directly process a single JD text or markdown file")
    parser.add_argument("--company", default="", help="Override company name for single JD run")
    parser.add_argument("--role", default="", help="Override role title for single JD run")
    args = parser.parse_args()

    if args.stats:
        print_state_summary()
        return

    if args.jd_file:
        path = Path(args.jd_file)
        if not path.exists():
            print(f"[bridge] ❌ File not found: {args.jd_file}")
            sys.exit(1)
        raw_text = path.read_text(encoding="utf-8")
        meta = extract_metadata_with_gemini(raw_text, path.name)
        company = args.company or meta.get("company", "TargetCompany")
        role = args.role or meta.get("role", "Product Manager")
        job = {
            "source": f"cli:{path.name}",
            "company": company,
            "role": role,
            "location": meta.get("location", "India"),
            "url": meta.get("url", ""),
            "date_posted": datetime.now().strftime("%Y-%m-%d"),
            "jd_text": raw_text
        }
        process_job(job, dry_run=args.dry_run, force=args.force)
        return

    run_bridge(dry_run=args.dry_run, manual_only=args.manual_only, force=args.force)


if __name__ == "__main__":
    main()
