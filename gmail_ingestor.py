"""
gmail_ingestor.py — PM Career Copilot: Gmail Alert Extractor & Google Resolver
==============================================================================
Sprint 5: Ingests inbound job opportunities from Gmail alerts and recruiter emails,
extracts clean structured leads via Gemini, resolves public Apply URLs and JDs via
direct tracking link unfurling and origin-aware portal search, and routes them
through the HITL evaluation & draft staging pipeline.

Core Architecture:
  1. Origin-Aware Portal Registry:
     Maps inbound sender domains (Wellfound, Naukri, IIMJobs, Cutshort, Instahyre, Indeed, LinkedIn)
     to portal-specific Google search filters and ATS domain scoring weights.
  2. Direct Tracking Link Unfurling:
     Preserves HTML hyperlinks from alert digests and unfurls HTTP redirects directly,
     discovering canonical job URLs with 0 search quota consumption.
  3. Structured Lead Extraction via Gemini:
     Splits multi-job digests into individual job leads and extracts candidate tracking URLs.
  4. Public Apply URL & JD Resolution:
     Scored by origin domain match (+50), ATS match (+60), and company career domain (+35).
  5. Pipeline Execution:
     Deduplicates in copilot_state.db, compiles bespoke 1-page PDF if fit_score >= 75,
     and stages warm outreach drafts in Gmail.
"""

import os
import io
import re
import json
import base64
import email.utils
import urllib.parse
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

import requests
from bs4 import BeautifulSoup
import pypdf
import docx

# Internal PM Copilot modules
ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

STATE_DB_PATH = ROOT / "copilot_state.db"

from dossier_bridge import (
    init_state_db,
    compute_job_hash,
    is_job_processed,
    record_processed_job,
    FIT_SCORE_THRESHOLD
)
from triage_and_build import (
    build_resume_for_jd,
    OUTPUT_RESUMES_DIR
)
from outreach_drafter import (
    get_gmail_service,
    create_gmail_draft,
    stage_outreach,
    call_gemini,
    search_serper
)

# ─────────────────────────────────────────────────────────────────────────────
# 1. GMAIL QUERY & PARSED EMAILS STATE TRACKING
# ─────────────────────────────────────────────────────────────────────────────

GMAIL_JOB_QUERY = (
    'newer_than:3d '
    '(from:(wellfound.com OR naukri.com OR iimjobs.com OR indeed.com OR linkedin.com OR instahyre.com OR cutshort.io) '
    'OR subject:("JD |" OR "Job Alert" OR "New jobs:" OR "Apply Now" OR "Founder\'s Office" OR "Product Manager" OR "Product Owner")) '
    '-subject:("MINIs" OR "resume might be saying" OR "career tips")'
)


def init_gmail_tables(conn=None):
    """Ensures parsed_emails tracking table exists in copilot_state.db."""
    should_close = False
    if conn is None:
        conn = init_state_db(STATE_DB_PATH)
        should_close = True

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
    conn.commit()
    if should_close:
        conn.close()


def is_email_parsed(msg_id: str, db_path: Path = STATE_DB_PATH) -> bool:
    """Checks if a Gmail message_id has already been processed."""
    conn = init_state_db(db_path)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM parsed_emails WHERE message_id = ?", (msg_id,))
    row = cur.fetchone()
    conn.close()
    return row is not None


def mark_email_parsed(msg_id: str, sender: str, subject: str, date_received: str, job_count: int, db_path: Path = STATE_DB_PATH):
    """Marks a Gmail message_id as parsed with job count and timestamp."""
    conn = init_state_db(db_path)
    with conn:
        conn.execute("""
            INSERT OR REPLACE INTO parsed_emails (message_id, sender, subject, date_received, job_count, parsed_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            msg_id,
            sender,
            subject,
            date_received,
            job_count,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ))
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# 2. ORIGIN-AWARE PORTAL REGISTRY & QUERY BUILDER
# ─────────────────────────────────────────────────────────────────────────────

PORTAL_CONFIGS = {
    "wellfound": {
        "label": "Wellfound / AngelList",
        "domains": ["wellfound.com", "angel.co"],
        "site_query": '(site:wellfound.com/jobs OR site:ashbyhq.com OR site:lever.co OR site:greenhouse.io OR "apply" OR "careers")',
        "job_path_patterns": ["wellfound.com/jobs", "angel.co/company"]
    },
    "naukri": {
        "label": "Naukri",
        "domains": ["naukri.com", "infoedge"],
        "site_query": '(site:naukri.com/job-listings OR site:linkedin.com/jobs OR "apply" OR "careers")',
        "job_path_patterns": ["naukri.com/job-listings", "naukri.com/job-"]
    },
    "iimjobs": {
        "label": "IIMJobs",
        "domains": ["iimjobs.com"],
        "site_query": '(site:iimjobs.com/j/ OR site:linkedin.com/jobs OR "apply" OR "careers")',
        "job_path_patterns": ["iimjobs.com/j/"]
    },
    "cutshort": {
        "label": "Cutshort",
        "domains": ["cutshort.io"],
        "site_query": '(site:cutshort.io/job OR site:linkedin.com/jobs OR "apply" OR "careers")',
        "job_path_patterns": ["cutshort.io/job"]
    },
    "instahyre": {
        "label": "Instahyre",
        "domains": ["instahyre.com"],
        "site_query": '(site:instahyre.com/job OR site:linkedin.com/jobs OR "apply" OR "careers")',
        "job_path_patterns": ["instahyre.com/job"]
    },
    "indeed": {
        "label": "Indeed",
        "domains": ["indeed.com", "indeed.co.in"],
        "site_query": '(site:indeed.com/viewjob OR site:indeed.co.in/viewjob OR site:linkedin.com/jobs OR "apply" OR "careers")',
        "job_path_patterns": ["indeed.com/viewjob", "indeed.co.in/viewjob", "indeed.com/rc/clk"]
    },
    "linkedin": {
        "label": "LinkedIn",
        "domains": ["linkedin.com"],
        "site_query": '(site:linkedin.com/jobs/view OR site:linkedin.com/jobs OR "apply" OR "careers")',
        "job_path_patterns": ["linkedin.com/jobs/view"]
    },
    "general": {
        "label": "General / Direct Recruiter",
        "domains": [],
        "site_query": '(site:greenhouse.io OR site:lever.co OR site:ashbyhq.com OR site:myworkdayjobs.com OR site:smartrecruiters.com OR site:linkedin.com/jobs OR "apply" OR "careers")',
        "job_path_patterns": []
    }
}


def detect_email_portal(sender_email: str, from_header: str = "", subject: str = "") -> str:
    """
    Detects the originating job portal from email headers, sender address, or subject.
    Returns portal key (e.g. 'wellfound', 'naukri', 'iimjobs', 'cutshort', 'instahyre', 'indeed', 'linkedin', or 'general').
    """
    haystack = f"{sender_email} {from_header} {subject}".lower()
    for portal_key, config in PORTAL_CONFIGS.items():
        if portal_key == "general":
            continue
        for dom in config["domains"]:
            if dom in haystack:
                return portal_key

    brand_keywords = [
        ("wellfound", "wellfound"),
        ("angellist", "wellfound"),
        ("naukri", "naukri"),
        ("iimjobs", "iimjobs"),
        ("cutshort", "cutshort"),
        ("instahyre", "instahyre"),
        ("indeed", "indeed"),
        ("linkedin", "linkedin"),
    ]
    for kw, key in brand_keywords:
        if kw in haystack:
            return key

    return "general"


# ─────────────────────────────────────────────────────────────────────────────
# 3. EMAIL PAYLOAD & ATTACHMENT EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

def decode_payload_data(data_str: str) -> str:
    """Decodes standard or urlsafe base64 string to utf-8 text."""
    if not data_str:
        return ""
    try:
        raw_bytes = base64.urlsafe_b64decode(data_str.encode("utf-8"))
        return raw_bytes.decode("utf-8", errors="replace")
    except Exception:
        try:
            raw_bytes = base64.b64decode(data_str.encode("utf-8"))
            return raw_bytes.decode("utf-8", errors="replace")
        except Exception:
            return ""


def extract_attachment_text(service, msg_id: str, part: dict) -> str:
    """Extracts text from .pdf or .docx attachment parts using pypdf or python-docx."""
    filename = part.get("filename", "")
    body_info = part.get("body", {})
    data_b64 = body_info.get("data")

    # If attachment data is stored externally, fetch via attachments API
    if not data_b64 and body_info.get("attachmentId"):
        try:
            att = service.users().messages().attachments().get(
                userId="me", messageId=msg_id, id=body_info["attachmentId"]
            ).execute()
            data_b64 = att.get("data")
        except Exception as e:
            print(f"[gmail_ingest] Error fetching attachment {filename}: {e}")
            return ""

    if not data_b64:
        return ""

    try:
        raw_bytes = base64.urlsafe_b64decode(data_b64.encode("utf-8"))
    except Exception:
        return ""

    ext = Path(filename).suffix.lower()
    text = ""

    if ext == ".pdf":
        try:
            reader = pypdf.PdfReader(io.BytesIO(raw_bytes))
            pages = [p.extract_text() or "" for p in reader.pages]
            text = "\n".join(pages).strip()
        except Exception as e:
            print(f"[gmail_ingest] pypdf parsing error for {filename}: {e}")
    elif ext in [".docx", ".doc"]:
        try:
            doc = docx.Document(io.BytesIO(raw_bytes))
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            text = "\n".join(paragraphs).strip()
        except Exception as e:
            print(f"[gmail_ingest] python-docx parsing error for {filename}: {e}")

    return text


def extract_message_body_and_attachments(service, msg_id: str, payload: dict) -> tuple[str, list[dict]]:
    """
    Recursively traverses MIME parts to extract plain text/HTML body and attachment texts.
    Formats HTML <a> tags with explicit URLs: 'Text (URL)' to preserve tracking links for unfurling.
    Returns (body_text, attachments_info).
    """
    plain_texts = []
    html_texts = []
    attachments = []

    def walk_parts(part: dict):
        mime_type = part.get("mimeType", "")
        filename = part.get("filename", "")

        # Check for attachments
        if filename and (filename.lower().endswith(".pdf") or filename.lower().endswith(".docx")):
            att_text = extract_attachment_text(service, msg_id, part)
            if att_text:
                attachments.append({"filename": filename, "text": att_text})

        # Check for message body text
        data_str = part.get("body", {}).get("data", "")
        if data_str:
            decoded = decode_payload_data(data_str)
            if mime_type == "text/plain":
                plain_texts.append(decoded)
            elif mime_type == "text/html":
                html_texts.append(decoded)

        # Recurse down subparts
        for subpart in part.get("parts", []):
            walk_parts(subpart)

    walk_parts(payload)

    # If HTML is available, format hyperlinks to preserve direct tracking URLs
    if html_texts:
        full_html = "\n".join(html_texts)
        soup = BeautifulSoup(full_html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            link_text = a.get_text().strip()
            if href.startswith("http") and link_text and href not in link_text:
                a.replace_with(f"{link_text} ({href}) ")
        for tag in soup(["script", "style", "head"]):
            tag.decompose()
        html_body = soup.get_text(separator="\n").strip()
        full_body = html_body if len(html_body) >= 50 else ("\n\n".join(plain_texts).strip() or html_body)
    elif plain_texts:
        full_body = "\n\n".join(plain_texts).strip()
    else:
        full_body = decode_payload_data(payload.get("body", {}).get("data", ""))

    return full_body, attachments


def parse_email_message(service, msg_id: str) -> dict | None:
    """Fetches and parses a single Gmail message into structured metadata and content."""
    try:
        msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    except Exception as e:
        print(f"[gmail_ingest] Error fetching message {msg_id}: {e}")
        return None

    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    from_header = headers.get("from", "")
    subject = headers.get("subject", "No Subject")
    date_header = headers.get("date", "")
    thread_id = msg.get("threadId", "")

    sender_name, sender_email = email.utils.parseaddr(from_header)
    sender_clean = sender_email.lower().strip()

    body_text, attachments = extract_message_body_and_attachments(service, msg_id, msg.get("payload", {}))
    detected_origin = detect_email_portal(sender_clean, from_header, subject)

    return {
        "msg_id": msg_id,
        "thread_id": thread_id,
        "from_header": from_header,
        "sender_name": sender_name,
        "sender_email": sender_clean,
        "subject": subject,
        "date_header": date_header,
        "body_text": body_text,
        "attachments": attachments,
        "detected_origin": detected_origin
    }


# ─────────────────────────────────────────────────────────────────────────────
# 4. GEMINI STRUCTURED JOB LEAD EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

def extract_leads_with_gemini(email_data: dict) -> list[dict]:
    """
    Uses Gemini to extract distinct job opportunities from email body + attachments.
    Filters out marketing/promo spam and splits multi-job digest emails.
    Extracts direct tracking / apply URLs associated with each job when available.
    """
    sender = email_data.get("from_header", "")
    subject = email_data.get("subject", "")
    body = email_data.get("body_text", "")
    attachments = email_data.get("attachments", [])

    att_summary = ""
    for a in attachments:
        att_summary += f"\n--- ATTACHMENT ({a['filename']}) ---\n{a['text'][:4000]}\n"

    # Truncate body if very long to stay within token context
    clean_body = body[:12000]

    prompt = f"""
You are an expert recruitment and HR data extraction system.
Extract all distinct job opportunities from the following email.

Sender: {sender}
Subject: {subject}

Email Content:
\"\"\"
{clean_body}
\"\"\"
{att_summary}

Extraction Instructions:
1. Identify all distinct job opportunities.
   - If this is an email digest containing multiple jobs (e.g. Wellfound "1 + 5 more jobs", Indeed "1 + 16 new jobs", Naukri, LinkedIn alert), split them into separate job items.
2. Filter out:
   - Marketing banners, promotional upsells, career coaching / resume-review ads, newsletters, platform announcements, and non-job spam.
3. For each legitimate job opportunity, extract:
   - "company": Canonical company name (e.g. "Swiggy", "Acme Analytics", "Zomato", "Standard Chartered").
   - "role": Exact role title (e.g. "Associate Product Manager", "Product Manager", "Product Owner", "Founder's Office").
   - "location": Job location (e.g. "Bengaluru, India", "Mumbai", "Remote", "India").
   - "is_direct_recruiter": Boolean (true if this email was personally written or sent by a direct recruiter/founder/hiring manager directly to the candidate; false if this is an automated digest/alert from a job platform like LinkedIn, Wellfound, Naukri, Indeed, Instahyre, Cutshort).
   - "recruiter_name": Recruiter's full name if direct recruiter, otherwise null.
   - "recruiter_email": Recruiter's direct email if direct recruiter, otherwise null.
   - "embedded_jd_text": Any detailed job description, responsibilities, or requirements text found in the email or attachment for this role. If only a short snippet is provided, include that snippet.
   - "job_url": Any specific link or tracking URL associated with this role in the email (e.g. from an Apply button/link or listing link), or null if not explicitly present.

Return ONLY a valid JSON array of objects with this schema:
[
  {{
    "company": "Company Name",
    "role": "Role Title",
    "location": "Location",
    "is_direct_recruiter": false,
    "recruiter_name": null,
    "recruiter_email": null,
    "embedded_jd_text": "...",
    "job_url": "https://..."
  }}
]

If NO relevant product or digital management jobs are present in the email, return an empty array: []
"""

    resp_text = call_gemini(prompt)
    if not resp_text:
        return []

    try:
        cleaned = re.sub(r"^```json\s*", "", resp_text.strip(), flags=re.MULTILINE)
        cleaned = re.sub(r"```$", "", cleaned.strip(), flags=re.MULTILINE)
        leads = json.loads(cleaned)
        if isinstance(leads, list):
            valid_leads = []
            for item in leads:
                if isinstance(item, dict) and item.get("company") and item.get("role"):
                    valid_leads.append(item)
            return valid_leads
    except Exception as e:
        print(f"[gemini_extract] JSON parse error: {e}")

    return []


# ─────────────────────────────────────────────────────────────────────────────
# 5. PUBLIC APPLY URL & JD RESOLVER (UNFURLING + GOOGLE SEARCH)
# ─────────────────────────────────────────────────────────────────────────────

def clean_tracking_params(url: str) -> str:
    """Removes marketing/click tracking parameters from job URLs."""
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlparse(url)
        query_dict = urllib.parse.parse_qs(parsed.query)
        tracking_keys = {
            "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
            "ref", "trackingid", "trk", "refid", "source", "midtoken", "ek", "trkcode",
            "gclid", "fbclid", "msclkid"
        }
        cleaned_query = {k: v for k, v in query_dict.items() if k.lower() not in tracking_keys}
        new_query = urllib.parse.urlencode(cleaned_query, doseq=True)
        return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, ""))
    except Exception:
        return url


def unfurl_redirect_url(url: str, timeout: float = 4.0) -> str:
    """
    Follows HTTP redirects (e.g. email tracking links from Wellfound, LinkedIn, Naukri)
    to discover the authentic destination job URL without downloading full page bodies.
    """
    if not url or not url.startswith("http"):
        return url

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }

    # Step 1: Fast HEAD request
    try:
        resp = requests.head(url, headers=headers, allow_redirects=True, timeout=timeout)
        if resp.url and resp.url.startswith("http") and resp.url != url:
            return clean_tracking_params(resp.url)
    except Exception:
        pass

    # Step 2: Streaming GET request fallback if HEAD is unsupported
    try:
        resp = requests.get(url, headers=headers, allow_redirects=True, stream=True, timeout=timeout)
        final_url = resp.url
        resp.close()
        if final_url and final_url.startswith("http"):
            return clean_tracking_params(final_url)
    except Exception:
        pass

    return clean_tracking_params(url)


def score_apply_url(url: str, company: str, origin: str = "general") -> int:
    """
    Scores how likely a search result or unfurled URL is to be an authentic public application page.
    Biases heavily towards:
      - Origin portal match (+50 for specific job listing path, +35 for origin domain)
      - Direct ATS links (+60)
      - Company careers pages (+35)
      - Company slug in URL (+25)
      - Specific job posting views on reputable Indian/global portals (+30)
    Penalizes non-job pages, aggregator search results, and review/media sites.
    """
    score = 0
    url_lower = url.lower()
    co_slug = re.sub(r"[^\w]", "", company.lower())

    # 1. Direct ATS links (Global & Indian tech ecosystem)
    ats_domains = [
        "greenhouse.io", "lever.co", "ashbyhq.com", "workday.com",
        "myworkdayjobs.com", "smartrecruiters.com", "bamboohr.com",
        "recruitee.com", "rippling-ats.com", "keka.com", "freshteam.com",
        "jobvite.com", "workable.com"
    ]
    if any(ats in url_lower for ats in ats_domains):
        score += 60

    # 2. Origin portal match boost
    if origin and origin != "general":
        cfg = PORTAL_CONFIGS.get(origin, {})
        if any(pat in url_lower for pat in cfg.get("job_path_patterns", [])):
            score += 60
        elif any(dom in url_lower for dom in cfg.get("domains", [])):
            score += 35

    # 3. Company career domain / path
    if "careers." in url_lower or "/careers" in url_lower or "/jobs" in url_lower or "/job/" in url_lower:
        score += 35

    # 4. Company name matched in domain or path
    if co_slug and co_slug in url_lower:
        score += 25

    # 5. Major job boards with specific job post views
    portal_job_views = [
        "linkedin.com/jobs/view", "iimjobs.com/j/", "naukri.com/job-listings",
        "wellfound.com/jobs", "cutshort.io/job", "instahyre.com/job",
        "indeed.com/viewjob", "indeed.co.in/viewjob"
    ]
    if any(view in url_lower for view in portal_job_views):
        score += 30

    # 6. Penalties for aggregator search listings, directories, and non-job pages
    penalties = [
        "/search", "google.com", "glassdoor.com/reviews", "wikipedia.org",
        "youtube.com", "facebook.com", "twitter.com", "instagram.com"
    ]
    if any(neg in url_lower for neg in penalties):
        score -= 50

    return score


def search_google_for_job(company: str, role: str, origin: str = "general") -> list[dict]:
    """
    Searches Google for clean public application URLs and JD snippets.
    Dynamically applies origin-aware portal and ATS filters based on the originating email platform.
    """
    portal_config = PORTAL_CONFIGS.get(origin, PORTAL_CONFIGS["general"])
    filter_part = portal_config["site_query"]
    query = f'"{company}" "{role}" {filter_part}'

    serper_key = os.environ.get("SERPER_API_KEY", "").strip()

    if serper_key:
        results = search_serper(query, serper_key)
        if results:
            return results

    # Fallback to DuckDuckGo search
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
    try:
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            results = []
            for res in soup.find_all("div", class_="result__body"):
                title_tag = res.find("a", class_="result__a")
                snippet_tag = res.find("a", class_="result__snippet")
                if title_tag:
                    link = title_tag.get("href", "")
                    title_text = title_tag.text.strip()
                    snippet = snippet_tag.text.strip() if snippet_tag else ""
                    results.append({"title": title_text, "snippet": snippet, "link": link})
            return results
    except Exception as e:
        print(f"[search] Web search fallback error: {e}")

    return []


def fetch_public_page_text(url: str) -> str:
    """Attempts to fetch readable text from a public job page without authentication."""
    if not url or not url.startswith("http"):
        return ""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code == 200 and "text/html" in resp.headers.get("Content-Type", ""):
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
                tag.decompose()
            text = soup.get_text(separator="\n")
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            cleaned = "\n".join(lines)
            if len(cleaned.split()) >= 60:
                return cleaned[:4500]
    except Exception:
        pass
    return ""


def resolve_job_details(lead: dict, origin: str = "general") -> dict:
    """
    Resolves clean public Apply URL and comprehensive JD text for an extracted lead:
      1. Direct Recruiter Check: If direct recruiter with rich JD, uses embedded text directly.
      2. Tracking Link Unfurling: If lead has job_url (extracted from email), unfurls redirects.
         If unfurled link scores >= 40, uses it directly (saving search quota & getting exact listing).
      3. Origin-Aware Google Search: Searches with portal-specific boolean filter.
      4. Fetches live page text or combines search snippets.
    """
    company = (lead.get("company") or "").strip()
    role = (lead.get("role") or "").strip()
    location = (lead.get("location") or "India").strip()
    embedded_jd = lead.get("embedded_jd_text") or ""
    is_direct = bool(lead.get("is_direct_recruiter", False))
    email_job_url = lead.get("job_url") or ""

    apply_url = ""
    resolved_jd = embedded_jd

    # 1. Direct Recruiter with rich embedded text (>= 80 words)
    if is_direct and len(embedded_jd.split()) >= 80:
        return {
            "company": company,
            "role": role,
            "location": lead.get("location", "India"),
            "url": email_job_url,
            "jd_text": resolved_jd,
            "is_direct_recruiter": True,
            "recruiter_name": lead.get("recruiter_name"),
            "recruiter_email": lead.get("recruiter_email")
        }

    # 2. Direct Link Unfurling from email tracking link
    if email_job_url and email_job_url.startswith("http"):
        print(f"[resolver] 🔗 Attempting direct link unfurling for {company} ({email_job_url[:55]}...)...")
        unfurled_url = unfurl_redirect_url(email_job_url)
        url_score = score_apply_url(unfurled_url, company, origin=origin)
        if url_score >= 40:
            apply_url = unfurled_url
            print(f"           ✅ Direct email link resolved: {apply_url[:65]}... (Score: {url_score})")
            page_text = fetch_public_page_text(apply_url)
            if page_text:
                resolved_jd = f"Job Title: {role}\nCompany: {company}\n\nJob Details:\n{page_text}"

    # 3. Origin-Aware Google Search if direct URL wasn't resolved
    if not apply_url:
        print(f"[resolver] 🔎 Searching Google ({origin}) for: {company} — {role}...")
        search_results = search_google_for_job(company, role, origin=origin)

        if search_results:
            scored_links = []
            for res in search_results:
                link = res.get("link", "")
                if link and link.startswith("http"):
                    clean_link = clean_tracking_params(link)
                    s = score_apply_url(clean_link, company, origin=origin)
                    scored_links.append((s, clean_link, res.get("snippet", "")))

            scored_links.sort(key=lambda x: x[0], reverse=True)

            if scored_links:
                best_score, best_url, best_snippet = scored_links[0]
                apply_url = best_url
                print(f"           Discovered URL: {apply_url[:65]}... (Score: {best_score})")

                # Try to fetch live page text
                page_text = fetch_public_page_text(apply_url)
                if page_text:
                    resolved_jd = f"Job Title: {role}\nCompany: {company}\n\nJob Details:\n{page_text}"
                else:
                    combined_snippets = "\n".join([f"- {r.get('title')}: {r.get('snippet')}" for r in search_results[:3]])
                    resolved_jd = f"Job Title: {role}\nCompany: {company}\n\n{embedded_jd}\n\nSearch Snippets:\n{combined_snippets}"

    if not resolved_jd or len(resolved_jd.split()) < 30:
        resolved_jd = f"Job Title: {role}\nCompany: {company}\nLocation: {location}\n{embedded_jd}"

    return {
        "company": company,
        "role": role,
        "location": location,
        "url": apply_url,
        "jd_text": resolved_jd,
        "is_direct_recruiter": is_direct,
        "recruiter_name": lead.get("recruiter_name"),
        "recruiter_email": lead.get("recruiter_email")
    }


# ─────────────────────────────────────────────────────────────────────────────
# 6. PIPELINE EXECUTION & DRAFT STAGING
# ─────────────────────────────────────────────────────────────────────────────

def process_resolved_lead(job: dict, source_label: str, thread_id: str | None = None, dry_run: bool = False, force: bool = False) -> dict:
    """
    Routes a resolved Gmail job lead through the PM Career Copilot pipeline:
      1. Deduplication check in copilot_state.db.
      2. Scoring via triage_and_build.py (Rule 5 rubric).
      3. If fit_score >= 75: Compiles bespoke 1-page PDF.
      4. If is_direct_recruiter: Stages direct reply draft to recruiter_email with PDF.
         Else: Executes Alumni / Hiring Manager outreach & stages Gmail draft.
      5. Records 15-column audit record in copilot_state.db.
    """
    company = (job.get("company") or "").strip()
    role = (job.get("role") or "").strip()
    location = (job.get("location") or "India").strip()
    url = (job.get("url") or "").strip()
    jd_text = (job.get("jd_text") or "").strip()
    is_direct = bool(job.get("is_direct_recruiter", False))
    recruiter_name = (job.get("recruiter_name") or "").strip()
    recruiter_email = (job.get("recruiter_email") or "").strip()

    # 1. Deduplication Check
    job_hash = compute_job_hash(company, role)
    if not force and is_job_processed(job_hash):
        print(f"[skip] ⏭️  Already in state DB: {company} — {role} (Hash: {job_hash[:10]}..)")
        return {"status": "SKIPPED", "company": company, "role": role}

    print(f"\n{'='*75}")
    print(f"  ✉️  GMAIL LEAD EVALUATION: {company} — {role}")
    print(f"      Source: {source_label} | Direct Recruiter: {is_direct}")
    print(f"{'='*75}")

    # 2. Fit Score & Resume Compilation
    triage_res = build_resume_for_jd(jd_text, company_name=company, role_title=role)
    fit_score = triage_res.get("fit_score", 0)
    verdict   = triage_res.get("verdict", "LOW_FIT")
    pdf_path  = triage_res.get("pdf_path")
    bullets   = triage_res.get("selected_bullets")

    # Gate: Score Threshold Check
    if fit_score < FIT_SCORE_THRESHOLD or verdict == "REJECT":
        print(f"[gate] ❌ REJECT: Fit score {fit_score} < threshold {FIT_SCORE_THRESHOLD} ({verdict}).")
        record = {
            "job_hash": job_hash,
            "source": source_label,
            "company": company,
            "role": role,
            "location": location,
            "url": url,
            "date_posted": datetime.now().strftime("%Y-%m-%d"),
            "fit_score": fit_score,
            "pdf_path": "",
            "contact_name": recruiter_name,
            "contact_title": "Recruiter" if is_direct else "",
            "contact_email": recruiter_email,
            "alum_tag": "Recruiter Direct" if is_direct else "None",
            "draft_status": "REJECTED"
        }
        record_processed_job(record)
        return {"status": "REJECTED", "fit_score": fit_score, "company": company, "role": role}

    print(f"[gate] ✅ ACCEPT: Fit score {fit_score} >= {FIT_SCORE_THRESHOLD} ({verdict})")
    print(f"       1-Page PDF generated: {pdf_path.name if pdf_path else 'None'}")

    top_bullet = ""
    if bullets and bullets.get("iqvia") and len(bullets["iqvia"]) > 0:
        top_bullet = bullets["iqvia"][0]

    # 3. Outreach Resolution & Staging
    if is_direct and recruiter_email:
        # Direct Recruiter Reply Flow
        first_name = recruiter_name.split()[0] if recruiter_name else "Hiring Team"
        subject = f"Re: {role} at {company} — Jaivik Chauhan"
        clean_proof = top_bullet.replace("**", "").replace(r"\textbf{", "").replace("}", "").strip()

        body = (
            f"Hi {first_name},\n\n"
            f"Thank you for reaching out regarding the {role} opportunity at {company}.\n\n"
            f"I bring a combined background in IT engineering from VIT Vellore (2023), general management MBA from IIM Indore (2025), "
            f"and consulting rigor at IQVIA driving 0-to-1 product development. Most recently, I {clean_proof.lower() if clean_proof.startswith('Led') or clean_proof.startswith('Built') else 'led high-impact product execution delivering measurable adoption'}.\n\n"
            f"I have attached my tailored 1-page resume for your review and would welcome 10–15 minutes this week for a brief conversation on team fit.\n\n"
            f"Best regards,\nJaivik Chauhan\n+91-7016175705 | linkedin.com/in/jaivik-chauhan"
        )

        if dry_run:
            draft_status = "DRY_RUN_STAGED"
            print(f"[gmail] 📝 [DRY-RUN] Direct recruiter reply staged for {recruiter_email}")
        else:
            draft_id = create_gmail_draft(recruiter_email, subject, body, pdf_path, thread_id=thread_id)
            draft_status = "LIVE_STAGED" if draft_id else "DRAFT_FAILED"
            print(f"[gmail] 🚀 Live draft created in Gmail (ID: {draft_id})")

        outreach_data = {
            "contact_name": recruiter_name or "Direct Recruiter",
            "contact_title": "Recruiter / Talent Partner",
            "contact_email": recruiter_email,
            "alum_tag": "Recruiter Direct",
            "draft_status": draft_status
        }
    else:
        # Standard Alumni Wedge & Cold Outreach Flow
        outreach_data = stage_outreach(
            company=company,
            role=role,
            jd_text=jd_text,
            pdf_path=pdf_path,
            top_bullet=top_bullet,
            dry_run=dry_run
        )

    # 4. Save to copilot_state.db
    record = {
        "job_hash": job_hash,
        "source": source_label,
        "company": company,
        "role": role,
        "location": location,
        "url": url,
        "date_posted": datetime.now().strftime("%Y-%m-%d"),
        "fit_score": fit_score,
        "pdf_path": str(pdf_path) if pdf_path else "",
        "contact_name": outreach_data.get("contact_name", ""),
        "contact_title": outreach_data.get("contact_title", ""),
        "contact_email": outreach_data.get("contact_email", ""),
        "alum_tag": outreach_data.get("alum_tag", "None"),
        "draft_status": outreach_data.get("draft_status", "PROCESSED")
    }
    record_processed_job(record)
    print(f"[state] 💾 Recorded {company} in copilot_state.db with status: {record['draft_status']}")

    return {
        "status": "COMPLETED",
        "company": company,
        "role": role,
        "fit_score": fit_score,
        "pdf_path": str(pdf_path) if pdf_path else "",
        "draft_status": record["draft_status"]
    }


# ─────────────────────────────────────────────────────────────────────────────
# 7. MASTER GMAIL INGESTION LOOP
# ─────────────────────────────────────────────────────────────────────────────

def run_gmail_ingestion(dry_run: bool = False, force: bool = False, max_emails: int = 25) -> list[dict]:
    """
    Main entrypoint for Gmail alert ingestion across Dual Accounts:
      1. Primary: jaivik.chauhan.2026@gmail.com (Naukri, Indeed, Wellfound, Instahyre)
      2. Secondary: jaivik.sedsvit@gmail.com (LinkedIn alerts, VIT inbox)
    All qualified drafts are staged into Primary with 'Job Copilot' label.
    """
    print(f"\n{'='*80}")
    print("  📬  DUAL-INBOX GMAIL JOB INGESTION ENGAGED")
    print("      Primary (Drafts/Portals): jaivik.chauhan.2026@gmail.com")
    print("      Secondary (LinkedIn/Alerts): jaivik.sedsvit@gmail.com")
    print(f"{'='*80}")

    init_gmail_tables()

    accounts_to_scan = [
        ("primary", "jaivik.chauhan.2026@gmail.com"),
        ("secondary", "jaivik.sedsvit@gmail.com")
    ]

    total_leads_evaluated = []

    for acct_key, acct_email in accounts_to_scan:
        service = get_gmail_service(account=acct_key)
        if not service:
            print(f"[gmail_ingest] ℹ️  {acct_key.capitalize()} account ({acct_email}) not yet authenticated or token missing. Skipping this inbox.")
            continue

        try:
            res = service.users().messages().list(userId="me", q=GMAIL_JOB_QUERY, maxResults=max_emails).execute()
            messages = res.get("messages", [])
        except Exception as e:
            print(f"[gmail_ingest] ❌ Error querying {acct_key} Gmail API ({acct_email}): {e}")
            continue

        if not messages:
            print(f"[gmail_ingest] ℹ️  No new matching job emails found in {acct_key} ({acct_email}).")
            continue

        print(f"\n[gmail_ingest] 📨 [{acct_key.upper()} - {acct_email}] Discovered {len(messages)} candidate emails.")

        for m in messages:
            msg_id = m.get("id")
            if not force and is_email_parsed(msg_id):
                continue

            email_data = parse_email_message(service, msg_id)
            if not email_data:
                continue

            sender_domain = email_data["sender_email"].split("@")[-1] if "@" in email_data["sender_email"] else "gmail"
            detected_origin = email_data.get("detected_origin", "general")
            source_label = f"gmail:{acct_key}:{detected_origin if detected_origin != 'general' else sender_domain}"
            print(f"\n[gmail_ingest] 📧 [{acct_key.upper()}] \"{email_data['subject'][:50]}...\" (Portal: {detected_origin.upper()})")

            leads = extract_leads_with_gemini(email_data)
            print(f"               Extracted {len(leads)} job lead(s) via Gemini.")

            for lead in leads:
                resolved_job = resolve_job_details(lead, origin=detected_origin)
                out = process_resolved_lead(
                    resolved_job,
                    source_label=source_label,
                    thread_id=email_data.get("thread_id"),
                    dry_run=dry_run,
                    force=force
                )
                total_leads_evaluated.append(out)

            mark_email_parsed(
                msg_id=msg_id,
                sender=email_data["from_header"],
                subject=email_data["subject"],
                date_received=email_data["date_header"],
                job_count=len(leads)
            )

    print(f"\n[gmail_ingest] 🏁 Completed Dual Gmail Pass: {len(total_leads_evaluated)} total job leads evaluated.")
    return total_leads_evaluated


# ─────────────────────────────────────────────────────────────────────────────
# 8. CLI INTERFACE
# ─────────────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="PM Career Copilot — Gmail Job Ingestor & Resolver")
    parser.add_argument("--dry-run", action="store_true", help="Run without creating live Gmail drafts")
    parser.add_argument("--force", action="store_true", help="Re-process emails and jobs even if already in state DB")
    parser.add_argument("--max", type=int, default=25, help="Maximum emails to inspect (default: 25)")
    parser.add_argument("--stats", action="store_true", help="Display parsed emails audit log")
    args = parser.parse_args()

    if args.stats:
        conn = init_state_db(STATE_DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT * FROM parsed_emails ORDER BY parsed_at DESC LIMIT 20")
        rows = cur.fetchall()
        conn.close()
        print(f"\n{'='*95}")
        print(f"  📬  PARSED GMAIL EMAILS AUDIT ({len(rows)} Records)")
        print(f"{'='*95}")
        print(f"{'Message ID':<18} {'Jobs':<6} {'Parsed At':<20} {'Subject'}")
        print(f"{'-'*18} {'-'*6} {'-'*20} {'-'*45}")
        for r in rows:
            print(f"{r['message_id']:<18} {r['job_count']:<6} {r['parsed_at']:<20} {r['subject'][:45]}")
        print(f"{'='*95}\n")
        return

    run_gmail_ingestion(dry_run=args.dry_run, force=args.force, max_emails=args.max)


if __name__ == "__main__":
    main()
