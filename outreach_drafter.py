"""
outreach_drafter.py — PM Career Copilot: X-Ray Contact Hunter & Gmail Draft Stager
==================================================================================
Sprint 3: The Intelligence & Outreach Execution Engine.

Modular Stages:
  1. Contact Discovery (Ban-Safe, Zero LinkedIn Login):
       - Regex scan for recruiter email in JD
       - Google X-Ray search (SERPER_API_KEY with DuckDuckGo fallback):
           Query 1 (Alum Wedge): site:linkedin.com/in/ "{company}" ("Product" OR "Founder") ("IIM Indore" OR "VIT")
           Query 2 (Decision Maker): site:linkedin.com/in/ "{company}" ("VP Product" OR "Head of Product" OR "Director of Product" OR "Group Product Manager")
       - Gemini structured extraction: full_name, title, linkedin_url, is_alum
  2. Corporate Email Resolution Waterfall:
       - Infer domain from JD link or company name
       - dnspython MX record verification
       - Generate high-probability formats: first@domain, first.last@domain
       - Optional SignalHire hook (withoutWaterfall=true)
  3. Hyper-Personalized Pitch & Gmail Draft Staging:
       - Gemini 4-sentence cold email (<110 words) tailored with top resume achievement
       - Gmail API OAuth (scope: https://www.googleapis.com/auth/gmail.compose)
       - Attaches bespoke 1-page PDF into user's Gmail Drafts (never auto-sends)
       - Supports --dry-run when credentials.json is not yet configured

Usage:
  python outreach_drafter.py --company "Acme Analytics" --role "Product Manager" --pdf "output/Resume_AcmeAnalytics.pdf" [--dry-run]
"""

import os
import sys
import re
import json
import base64
import urllib.parse
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

import requests
from bs4 import BeautifulSoup
import dns.resolver

# Google OAuth & Gmail API imports
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

try:
    from google import genai
except ImportError:
    genai = None

# Constants
ROOT = Path(__file__).parent
CREDENTIALS_FILE     = ROOT / "credentials.json"
TOKEN_PRIMARY_FILE   = ROOT / "token_primary.json"
TOKEN_SECONDARY_FILE = ROOT / "token_secondary.json"
TOKEN_FILE           = ROOT / "token_primary.json"

GMAIL_PRIMARY_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify"
]
GMAIL_SECONDARY_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose"
]
GMAIL_SCOPES = GMAIL_PRIMARY_SCOPES
PREFERRED_MODELS = ["gemini-flash-lite-latest", "gemini-3.8-flash"]


def get_credentials_path() -> Path | None:
    """Finds the Google OAuth credentials JSON file (whether credentials.json or credential.json, file or directory)."""
    for candidate in [ROOT / "credentials.json", ROOT / "credential.json"]:
        if candidate.is_file():
            return candidate
        if candidate.is_dir():
            files = list(candidate.glob("*.json"))
            if files:
                return files[0]
    root_secrets = list(ROOT.glob("client_secret*.json"))
    if root_secrets:
        return root_secrets[0]
    return None


# ─────────────────────────────────────────────────────────────────────────────
# GEMINI CLIENT HELPER
# ─────────────────────────────────────────────────────────────────────────────

def call_gemini(prompt: str) -> str:
    """Invokes Gemini using available API key with multi-model fallback."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key or not genai:
        return ""

    client = genai.Client(api_key=api_key)
    for model_name in PREFERRED_MODELS:
        try:
            resp = client.models.generate_content(model=model_name, contents=prompt)
            if resp and resp.text:
                return resp.text.strip()
        except Exception:
            continue
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 1: CONTACT DISCOVERY (BAN-SAFE GOOGLE X-RAY SEARCH)
# ─────────────────────────────────────────────────────────────────────────────

def extract_email_from_text(text: str) -> str | None:
    """Scans text for explicit recruiter email addresses, filtering out spam/system domains."""
    pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,7}\b'
    matches = re.findall(pattern, text)
    ignore_domains = [
        "indeed.com", "greenhouse.io", "lever.co", "workday.com",
        "linkedin.com", "glassdoor.com", "example.com", "sentry.io"
    ]
    for email in matches:
        domain = email.split("@")[-1].lower()
        if not any(ign in domain for ign in ignore_domains) and not email.startswith("no-reply") and not email.startswith("noreply"):
            return email
    return None


def search_serper(query: str, api_key: str) -> list[dict]:
    """Runs search via Serper.dev Google Search API."""
    url = "https://google.serper.dev/search"
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    payload = {"q": query, "num": 5}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        if r.status_code == 200:
            data = r.json()
            results = []
            for item in data.get("organic", []):
                results.append({
                    "title": item.get("title", ""),
                    "snippet": item.get("snippet", ""),
                    "link": item.get("link", "")
                })
            return results
    except Exception as e:
        print(f"[xray] Serper search error: {e}")
    return []


def search_duckduckgo(query: str) -> list[dict]:
    """Runs fallback web search via DuckDuckGo HTML endpoint without browser automation."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            results = []
            for res in soup.find_all("div", class_="result__body"):
                title_tag = res.find("a", class_="result__url")
                snippet_tag = res.find("a", class_="result__snippet")
                title = res.find("a", class_="result__a")
                link = title.get("href", "") if title else (title_tag.get("href", "") if title_tag else "")
                snippet = snippet_tag.text.strip() if snippet_tag else ""
                title_text = title.text.strip() if title else ""
                if "linkedin.com/in/" in link or "linkedin.com/in/" in title_text or "linkedin.com/in/" in snippet:
                    results.append({"title": title_text, "snippet": snippet, "link": link})
            return results
    except Exception as e:
        print(f"[xray] DuckDuckGo search fallback notice: {e}")
    return []


def parse_xray_snippet_with_gemini(snippets: list[dict], company: str) -> dict | None:
    """Uses Gemini to parse search snippets into a structured LinkedIn profile."""
    if not snippets:
        return None

    raw_blob = "\n".join([f"- Title: {s['title']} | Link: {s['link']}\n  Snippet: {s['snippet']}" for s in snippets[:4]])
    prompt = f"""
You are an expert executive talent researcher. Analyze the following Google X-Ray search results for leaders or product people at "{company}".

Search Results:
{raw_blob}

Extract the single most relevant contact (prioritize alumni from IIM Indore or VIT Vellore first, then VP Product, Head of Product, Director of Product, or Founder).
Output ONLY valid JSON with this exact schema:
{{
  "full_name": "First Last",
  "first_name": "First",
  "last_name": "Last",
  "title": "Exact Title from snippet",
  "linkedin_url": "https://www.linkedin.com/in/username",
  "is_alum": "IIM Indore" | "VIT Vellore" | "None",
  "is_named_person": true
}}
If no credible individual profile is identified, return null.
"""
    resp_text = call_gemini(prompt)
    if resp_text:
        try:
            cleaned = re.sub(r"^```json\s*", "", resp_text.strip(), flags=re.MULTILINE)
            cleaned = re.sub(r"```$", "", cleaned.strip(), flags=re.MULTILINE)
            data = json.loads(cleaned)
            if data and data.get("full_name") and data["full_name"].lower() != "null":
                data["is_named_person"] = True
                return data
        except Exception:
            pass

    # Heuristic fallback parser from snippet titles if LLM unavailable
    for s in snippets:
        title = s.get("title", "")
        if " - " in title and "linkedin" in title.lower():
            name_part = title.split(" - ")[0].strip()
            name_part = name_part.split(" | ")[0].strip()
            title_part = title.split(" - ")[1].strip() if len(title.split(" - ")) > 1 else "Product Leader"
            names = name_part.split()
            first = names[0] if names else "Product"
            last = names[-1] if len(names) > 1 else "Leader"
            alum = "IIM Indore" if "iim indore" in s.get("snippet", "").lower() else ("VIT Vellore" if "vit" in s.get("snippet", "").lower() else "None")
            return {
                "full_name": name_part,
                "first_name": first,
                "last_name": last,
                "title": title_part,
                "linkedin_url": s.get("link", f"https://www.linkedin.com/in/{first.lower()}-{last.lower()}"),
                "is_alum": alum,
                "is_named_person": True
            }

    return None


def hunt_contact(company: str, jd_text: str = "") -> dict:
    """
    Modular Stage 1: Runs Ban-Safe X-Ray Discovery.
    Returns: dict with full_name, first_name, last_name, title, linkedin_url, is_alum, is_named_person, source
    """
    # 1. Check JD for recruiter email
    recruiter_email = extract_email_from_text(jd_text)
    if recruiter_email:
        name_local = recruiter_email.split("@")[0].replace(".", " ").title()
        is_generic = any(gen in name_local.lower() for gen in ["career", "job", "hiring", "team", "talent", "recruit", "info"])
        return {
            "full_name": name_local,
            "first_name": name_local.split()[0] if not is_generic else "",
            "last_name": name_local.split()[-1] if (len(name_local.split()) > 1 and not is_generic) else "",
            "title": "Recruiter / Hiring Team",
            "linkedin_url": "",
            "is_alum": "None",
            "is_named_person": not is_generic,
            "direct_email": recruiter_email,
            "source": "JD Text"
        }

    # 2. Google X-Ray Search Queries
    serper_key = os.environ.get("SERPER_API_KEY", "").strip()

    # Query 1: Alumni Wedge
    q1 = f'site:linkedin.com/in/ "{company}" ("Product" OR "Founder" OR "Strategy") ("IIM Indore" OR "VIT")'
    print(f"[xray] Query 1 (Alum Wedge): {q1}")
    res1 = search_serper(q1, serper_key) if serper_key else search_duckduckgo(q1)

    contact = parse_xray_snippet_with_gemini(res1, company) if res1 else None
    if contact and contact.get("is_alum") != "None":
        contact["source"] = "X-Ray Alumni Wedge"
        contact["is_named_person"] = True
        return contact

    # Query 2: Product Decision Maker Fallback
    q2 = f'site:linkedin.com/in/ "{company}" ("VP Product" OR "Head of Product" OR "Director of Product" OR "Group Product Manager")'
    print(f"[xray] Query 2 (Decision Maker): {q2}")
    res2 = search_serper(q2, serper_key) if serper_key else search_duckduckgo(q2)

    contact2 = parse_xray_snippet_with_gemini(res2, company) if res2 else None
    if contact2:
        contact2["source"] = "X-Ray Decision Maker"
        contact2["is_named_person"] = True
        return contact2

    # Deterministic fallback leader for the company if no search results returned
    co_clean = re.sub(r"[^\w]", "", company.title())
    return {
        "full_name": f"{company} Product Team",
        "first_name": "",
        "last_name": "",
        "title": f"Head of Product / Hiring Manager at {company}",
        "linkedin_url": f"https://www.linkedin.com/company/{co_clean.lower()}",
        "is_alum": "None",
        "is_named_person": False,
        "source": "Company Leadership Heuristic"
    }


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 2: CORPORATE EMAIL RESOLUTION & MX WATERFALL
# ─────────────────────────────────────────────────────────────────────────────

def infer_company_domain(company: str, jd_url: str = "") -> str:
    """Infers primary web domain from job URL or company name."""
    if jd_url:
        try:
            parsed = urllib.parse.urlparse(jd_url)
            host = parsed.netloc.lower()
            # If not an ATS aggregator, host is the company domain
            aggregators = ["lever.co", "greenhouse.io", "workday.com", "ashbyhq.com", "bamboohr.com", "smartrecruiters.com"]
            if not any(agg in host for agg in aggregators):
                clean_host = re.sub(r"^www\.", "", host)
                if "." in clean_host:
                    return clean_host
            else:
                # E.g. jobs.lever.co/acmeanalytics -> acmeanalytics.com
                parts = parsed.path.strip("/").split("/")
                if parts and parts[0]:
                    slug = re.sub(r"[^\w]", "", parts[0].lower())
                    return f"{slug}.com"
        except Exception:
            pass

    clean_slug = re.sub(r"[^\w]", "", company.lower())
    return f"{clean_slug}.com"


def verify_domain_mx(domain: str) -> bool:
    """Verifies that the target company domain has active MX mail exchange records."""
    try:
        answers = dns.resolver.resolve(domain, "MX")
        return len(answers) > 0
    except Exception:
        return False


def query_signalhire_api(linkedin_url: str, full_name: str, company: str) -> str | None:
    """Optional hook for SignalHire Candidate API with withoutWaterfall=true."""
    api_key = os.environ.get("SIGNALHIRE_API_KEY", "").strip()
    if not api_key:
        return None

    url = "https://www.signalhire.com/api/v1/candidate/search"
    headers = {"apiKey": api_key, "Content-Type": "application/json"}
    payload = {
        "items": [linkedin_url or f"{full_name} {company}"],
        "withoutWaterfall": True
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=8)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and data and "candidate" in data[0]:
                emails = data[0]["candidate"].get("emails", [])
                for e in emails:
                    if e.get("type") == "corporate":
                        return e.get("email")
                if emails:
                    return emails[0].get("email")
    except Exception as e:
        print(f"[signalhire] Notice: {e}")
    return None


def resolve_corporate_email(contact: dict, company: str, jd_url: str = "") -> dict:
    """
    Modular Stage 2: Generates candidate email patterns and verifies MX.
    Returns: dict with primary_email, email_candidates, domain, mx_valid
    """
    if contact.get("direct_email"):
        return {
            "primary_email": contact["direct_email"],
            "email_candidates": [contact["direct_email"]],
            "domain": contact["direct_email"].split("@")[-1],
            "mx_valid": True,
            "method": "Direct from JD"
        }

    domain = infer_company_domain(company, jd_url)
    mx_valid = verify_domain_mx(domain)

    # Optional SignalHire Hook
    sh_email = query_signalhire_api(contact.get("linkedin_url", ""), contact.get("full_name", ""), company)
    if sh_email:
        return {
            "primary_email": sh_email,
            "email_candidates": [sh_email],
            "domain": sh_email.split("@")[-1],
            "mx_valid": True,
            "method": "SignalHire API"
        }

    first = re.sub(r"[^\w]", "", (contact.get("first_name") or "").lower())
    last  = re.sub(r"[^\w]", "", (contact.get("last_name") or "").lower())

    if not first or first in ["team", "product", "hiring", "recruiter", "careers"]:
        p1 = f"careers@{domain}"
        p2 = f"hiring@{domain}"
        candidates = [p1, p2]
    else:
        p1 = f"{first}.{last}@{domain}" if last else f"{first}@{domain}"
        p2 = f"{first}@{domain}"
        candidates = [p1, p2] if p1 != p2 else [p1]

    return {
        "primary_email": p1,
        "email_candidates": candidates,
        "domain": domain,
        "mx_valid": mx_valid,
        "method": "Corporate MX Waterfall"
    }


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 3: HYPER-PERSONALIZED PITCH & GMAIL DRAFT STAGING
# ─────────────────────────────────────────────────────────────────────────────

def generate_cold_pitch(contact: dict, company: str, role: str, top_bullet: str) -> dict:
    """
    Generates a crisp 4-sentence cold email under 110 words via Gemini.
      - Uses proper greeting: 'Hi {first_name},' for named person, or 'Hi {company} Product Team,' if generic.
      - Naturally paraphrases the top matched achievement in 1 conversational sentence (preserving exact metrics).
      - Applies correct possessive grammar (e.g. Acme Analytics' vs Acme Corp's).
    """
    is_alum = contact.get("is_alum") or contact.get("alum_tag") or "None"
    is_named = contact.get("is_named_person", True)

    first_name = contact.get("first_name", "").strip()
    if is_named and first_name and first_name.lower() not in ["hiring", "team", "product", "recruiter"]:
        greeting = f"Hi {first_name},"
    else:
        greeting = f"Hi {company} Product Team,"

    # Clean possessive punctuation
    company_possessive = f"{company}'" if company.rstrip().endswith(("s", "S")) else f"{company}'s"

    senior_article = "an" if is_alum.strip().lower().startswith(("i", "a", "e", "o", "u")) else "a"

    prompt = f"""
You are writing a hyper-personalized, authentic, human cold outreach email for Jaivik Chauhan applying for the {role} role at {company}.
Candidate Persona:
- Jaivik Chauhan | MBA from IIM Indore (2025) + B.Tech IT from VIT Vellore (2023)
- Current: Associate Consultant at IQVIA in Strategy Consulting
- Transition Goal: Actively transitioning from strategy consulting into Product Management (seeking advice/guidance and internal referral)
- Hands-on Builder: Passionate about building products since engineering days; actively builds product prototypes with modern AI agents and vibe coding; implemented workflow automations and product improvements at IQVIA.
- Top Resume Proof Point: "{top_bullet}"

Recipient Details:
- Name: {contact.get('full_name')}
- Title: {contact.get('title')}
- Alum status: {is_alum} (e.g. "IIM Indore", "VIT Vellore", or "None")
- Is Named Individual: {is_named}

Format: Write a crisp, highly readable cold email with short paragraphs and 2 bullet pointers (under 120 words total).
Tone Guidelines:
- Keep it natural, human, collegial, and humble.
- NEVER use generic corporate AI buzzwords like "consulting rigor", "0-to-1 digital product execution", or "align with your product goals".

Structure & Rules:
- Greeting: Start exactly with "{greeting}" (NEVER use "Hi Hiring," or "Hi Team,").
- Opening Paragraph:
  * If {is_alum} != "None": "Always great to connect with {senior_article} {is_alum} senior! I was searching for alums doing great work in product on LinkedIn and came across your profile, and also noticed the open {role} role at {company}."
  * If {is_alum} == "None": Mention following {company_possessive} product momentum with great interest and noticing the open {role} position on the team.
- Transition Line & 2 Bullet Pointers:
  * Intro line: "I'm currently at IQVIA in strategy consulting and actively working to transition into product management. A quick snapshot:"
  * Bullet 1: "• Background: B.Tech in IT (VIT Vellore) + MBA (IIM Indore '25)"
  * Bullet 2: "• Hands-on Builder: Passionate about product since engineering; actively building prototypes with AI agents / vibe coding, and driving workflow automations at IQVIA"
- Closing Ask Paragraph:
  * If {is_alum} != "None": "I’ve attached my tailored 1-page resume—would you be open to submitting an internal referral for this opening, or pointing me toward the hiring manager?"
  * If {is_alum} == "None": "I have attached my tailored 1-page resume—would you or the product hiring team be open to a brief conversation on team fit for this role?"
- Sign-off:
  * If {is_alum} != "None": End with "Thank you so much,\\nJaivik Chauhan\\n+91-7016175705 | linkedin.com/in/jaivik-chauhan"
  * If {is_alum} == "None": End with "Best regards,\\nJaivik Chauhan\\n+91-7016175705 | linkedin.com/in/jaivik-chauhan"

Subject Line Rules:
- If {is_alum} != "None": "Fellow {is_alum} alum — {role} opening at {company}"
- If {is_alum} == "None": "{role} opening at {company} — Jaivik Chauhan"

Return ONLY a JSON object:
{{
  "subject": "Subject Line",
  "body": "{greeting}\\n\\n[Opening Paragraph]\\n\\n[Transition Line]\\n• Background: ...\\n• Hands-on Builder: ...\\n\\n[Closing Ask]\\n\\n[Sign-off]"
}}
"""
    resp_text = call_gemini(prompt)
    if resp_text:
        try:
            cleaned = re.sub(r"^```json\s*", "", resp_text.strip(), flags=re.MULTILINE)
            cleaned = re.sub(r"```$", "", cleaned.strip(), flags=re.MULTILINE)
            data = json.loads(cleaned)
            if data and data.get("body") and data.get("subject"):
                data["subject"] = data["subject"].strip()
                body = data["body"]
                # Grammar & greeting cleanup
                body = re.sub(rf"{re.escape(company)}'s\b", company_possessive, body)
                if not is_named:
                    body = re.sub(r"^Hi Hiring,\s*", f"{greeting}\n\n", body)
                    body = re.sub(r"^Hi Team,\s*", f"{greeting}\n\n", body)
                data["body"] = body
                return data
        except Exception:
            pass

    # Deterministic High-Converting Paragraph + Bullet Pointer Fallback
    if is_alum != "None":
        subj = f"Fellow {is_alum} alum — {role} opening at {company}"
        p1 = f"Always great to connect with {senior_article} {is_alum} senior! I was searching for alums doing great work in product on LinkedIn and came across your profile, and also noticed the open {role} role at {company}."
        p2 = ("I'm currently at IQVIA in strategy consulting and actively working to transition into product management. A quick snapshot:\n\n"
              "• Background: B.Tech in IT (VIT Vellore) + MBA (IIM Indore '25)\n"
              "• Hands-on Builder: Passionate about product since engineering; actively building prototypes with AI agents / vibe coding, and driving workflow automations at IQVIA")
        p3 = "I’ve attached my tailored 1-page resume—would you be open to submitting an internal referral for this opening, or pointing me toward the hiring manager?"
        signoff = "Thank you so much,"
    else:
        subj = f"{role} opening at {company} — Jaivik Chauhan"
        p1 = f"Hope you're having a productive week; I’ve been closely following {company_possessive} product momentum and noticed the open {role} position on the team."
        p2 = ("I'm currently in strategy consulting at IQVIA and actively transitioning into digital product management. A quick snapshot:\n\n"
              "• Background: B.Tech in IT (VIT Vellore) + MBA (IIM Indore '25)\n"
              "• Hands-on Builder: Passionate about product since engineering; actively developing prototypes with AI agents / vibe coding and automating workflows at IQVIA")
        p3 = "I have attached my tailored 1-page resume—would you or the product hiring team be open to a brief conversation on team fit for this role?"
        signoff = "Best regards,"

    body = f"{greeting}\n\n{p1}\n\n{p2}\n\n{p3}\n\n{signoff}\nJaivik Chauhan\n+91-7016175705 | linkedin.com/in/jaivik-chauhan"
    return {"subject": subj, "body": body}


def get_gmail_service(account: str = "primary", interactive: bool = False):
    """
    Authenticates with Gmail API for a specific account ('primary' or 'secondary').
    - 'primary': jaivik.chauhan.2026@gmail.com (outreach drafting & primary alert ingestion)
    - 'secondary': jaivik.sedsvit@gmail.com (LinkedIn alerts & secondary ingestion)
    """
    cred_path = get_credentials_path()
    if not cred_path:
        return None

    if account == "secondary":
        token_path = TOKEN_SECONDARY_FILE
        scopes = GMAIL_SECONDARY_SCOPES
    else:
        token_path = TOKEN_PRIMARY_FILE
        scopes = GMAIL_PRIMARY_SCOPES

    # Backward compatibility: fallback to token.json if primary token doesn't exist yet
    if not token_path.exists() and (ROOT / "token.json").exists():
        token_path = ROOT / "token.json"
        scopes = GMAIL_SECONDARY_SCOPES

    creds = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), scopes)
        except Exception:
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open(token_path, "w", encoding="utf-8") as token_f:
                    token_f.write(creds.to_json())
            except Exception:
                creds = None

        if not creds:
            if not interactive:
                print(f"[gmail] ℹ️  {token_path.name} not found or expired. Run 'python auth_gmail_server.py --account {account}' to authorize.")
                return None
            try:
                flow = InstalledAppFlow.from_client_secrets_file(str(cred_path), scopes)
                creds = flow.run_local_server(port=0)
                with open(token_path, "w", encoding="utf-8") as token_f:
                    token_f.write(creds.to_json())
            except Exception as e:
                print(f"[gmail] OAuth error: {e}")
                return None

    return build("gmail", "v1", credentials=creds)


def get_or_create_label(service, label_name: str = "Job Copilot") -> str | None:
    """Finds or creates a Gmail user label, returning its labelId."""
    if not service:
        return None
    try:
        results = service.users().labels().list(userId="me").execute()
        labels = results.get("labels", [])
        for lbl in labels:
            if lbl.get("name", "").lower() == label_name.lower():
                return lbl.get("id")

        label_body = {
            "name": label_name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show"
        }
        created = service.users().labels().create(userId="me", body=label_body).execute()
        return created.get("id")
    except Exception:
        return None


def create_gmail_draft(to_email: str, subject: str, body_text: str, pdf_path: Path | None, thread_id: str | None = None) -> str | None:
    """Creates a draft in primary Gmail with attached PDF resume and tags it with 'Job Copilot' label."""
    service = get_gmail_service(account="primary")
    if not service:
        return None

    message = MIMEMultipart()
    message["to"] = to_email
    message["subject"] = subject

    # Attach email body
    message.attach(MIMEText(body_text, "plain"))

    # Attach PDF resume if available
    if pdf_path and Path(pdf_path).exists():
        with open(pdf_path, "rb") as f:
            pdf_data = f.read()
        part = MIMEBase("application", "pdf")
        part.set_payload(pdf_data)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{Path(pdf_path).name}"')
        message.attach(part)

    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    msg_dict = {"raw": raw_message}
    if thread_id:
        msg_dict["threadId"] = thread_id
    draft_body = {"message": msg_dict}

    try:
        draft = service.users().drafts().create(userId="me", body=draft_body).execute()
        draft_id = draft.get("id")

        # Tag draft message with 'Job Copilot' label
        msg_id = draft.get("message", {}).get("id")
        if msg_id:
            label_id = get_or_create_label(service, "Job Copilot")
            if label_id:
                try:
                    service.users().messages().modify(
                        userId="me",
                        id=msg_id,
                        body={"addLabelIds": [label_id]}
                    ).execute()
                except Exception:
                    pass

        return draft_id
    except Exception as e:
        print(f"[gmail] Draft creation API error: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# ORCHESTRATION PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

def stage_outreach(company: str, role: str, jd_text: str = "", pdf_path: Path | None = None, top_bullet: str = "", dry_run: bool = False) -> dict:
    """
    Executes the 3 modular stages of outreach:
      1. Contact Discovery (X-Ray / JD regex)
      2. Corporate Email Resolution (MX verification)
      3. Cold Email Generation & Gmail Draft Staging
    """
    print(f"\n{'='*60}")
    print(f"  🎯  OUTREACH STAGING: {company} — {role}")
    print(f"{'='*60}")

    # Stage 1: Contact Discovery
    print(f"[stage 1] Hunting decision maker / alumni contact for {company}...")
    contact = hunt_contact(company, jd_text)
    print(f"          Discovered: {contact['full_name']} ({contact['title']})")
    print(f"          Alum Tag  : {contact.get('is_alum','None')} | Source: {contact.get('source','')}")
    if contact.get("linkedin_url"):
        print(f"          LinkedIn  : {contact['linkedin_url']}")

    # Stage 2: Corporate Email Resolution
    print(f"\n[stage 2] Resolving corporate email & checking MX records...")
    email_res = resolve_corporate_email(contact, company)
    print(f"          Primary Candidate : {email_res['primary_email']}")
    print(f"          Domain & MX Valid : {email_res['domain']} [{'ACTIVE MX' if email_res['mx_valid'] else 'NO MX'}]")

    # Stage 3: Cold Pitch Generation
    default_bullet = "Led 0-to-1 development of an internal doctor-chemist mapping product powering territory planning, delivering ~50% higher accuracy"
    active_bullet = top_bullet or default_bullet

    print(f"\n[stage 3] Crafting hyper-personalized 4-sentence cold email (<110 words)...")
    pitch = generate_cold_pitch(contact, company, role, active_bullet)

    print(f"\n{'─'*55}")
    print(f"  SUBJECT : {pitch['subject']}")
    print(f"  TO      : {email_res['primary_email']}")
    print(f"  ATTACH  : {pdf_path.name if pdf_path else 'None'}")
    print(f"{'─'*55}")
    print(pitch["body"])
    print(f"{'─'*55}")

    # Safety Guard: Skip bounce-prone emails when NO MX and contact is not a verified named individual
    is_named = contact.get("is_named_person", True)
    if not email_res["mx_valid"] and not is_named:
        print(f"\n[stage 3] ⚠️  [NO MX] and contact is unverified/generic company team.")
        print(f"          Skipping bounce-prone email staging to protect domain reputation.")
        print(f"          Status assigned: NEEDS_MANUAL_CONTACT")
        return {
            "company": company,
            "role": role,
            "contact_name": contact["full_name"],
            "contact_title": contact["title"],
            "contact_email": email_res["primary_email"],
            "alum_tag": contact.get("is_alum", "None"),
            "subject": pitch["subject"],
            "body": pitch["body"],
            "draft_id": None,
            "draft_status": "NEEDS_MANUAL_CONTACT",
            "pdf_path": str(pdf_path) if pdf_path else ""
        }

    draft_id = None
    cred_path = get_credentials_path()
    if dry_run or not cred_path:
        print(f"\n[dry-run] ✅ Dry-run mode active. No live Gmail API draft created.")
        if not cred_path:
            print(f"[note]    To enable live staging, place your Google Cloud 'credentials.json' in root.")
        draft_status = "DRY_RUN_STAGED"
    else:
        print(f"\n[stage 3] Staging draft in Gmail...")
        draft_id = create_gmail_draft(email_res["primary_email"], pitch["subject"], pitch["body"], pdf_path)
        if draft_id:
            print(f"          🎉 Live Gmail Draft staged successfully! Draft ID: {draft_id}")
            draft_status = f"LIVE_STAGED:{draft_id}"
        else:
            print(f"          ⚠️ Could not stage live draft. Saved to dry-run state.")
            draft_status = "STAGING_FAILED"

    return {
        "company": company,
        "role": role,
        "contact_name": contact["full_name"],
        "contact_title": contact["title"],
        "contact_email": email_res["primary_email"],
        "alum_tag": contact.get("is_alum", "None"),
        "subject": pitch["subject"],
        "body": pitch["body"],
        "draft_id": draft_id,
        "draft_status": draft_status,
        "pdf_path": str(pdf_path) if pdf_path else ""
    }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="PM Career Copilot — Outreach Drafter")
    parser.add_argument("--company", required=True, help="Target company name")
    parser.add_argument("--role",    default="Product Manager", help="Target role title")
    parser.add_argument("--jd-file", default="", help="Optional path to JD text file")
    parser.add_argument("--pdf",     default="", help="Path to compiled resume PDF to attach")
    parser.add_argument("--bullet",  default="", help="Top bullet text proof point")
    parser.add_argument("--dry-run", action="store_true", help="Print draft without creating live Gmail draft")
    args = parser.parse_args()

    jd_text = Path(args.jd_file).read_text(encoding="utf-8") if args.jd_file and Path(args.jd_file).exists() else ""
    pdf_path = Path(args.pdf) if args.pdf and Path(args.pdf).exists() else None

    stage_outreach(args.company, args.role, jd_text, pdf_path, args.bullet, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
