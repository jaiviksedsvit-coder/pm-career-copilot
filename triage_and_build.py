"""
triage_and_build.py — PM Career Copilot: Intelligence & Compilation Engine
===========================================================================
Sprint 2 (v2.2 QA-Audited). Given a Job Description text file, it:
  1. Loads master_bank.json (structured by section, 11 IQVIA, 5 Bain, 3 JLL, 4 Projects)
  2. Computes a DETERMINISTIC fit_score (0–100) from three sub-scores:
       - yoe_and_role_score (0–30 pts)
       - competency_overlap_score (0–50 pts)
       - domain_bridge_score (0–20 pts)
  3. Enforces strict Top-3 Slot-Ranking for IQVIA:
       - Slot 1 (Anchor): Always iqvia_0to1_mapping
       - Slots 2 & 3 (Hook): 2 bullets with highest semantic overlap with JD archetype
       - Slots 4 to 8 (Impact): Top remaining commercial/growth bullets
  4. Selects:
       - 8 IQVIA bullets (slot-ranked)
       - 3 Bain bullets (top-3)
       - 3 JLL bullets (all-3, ranked)
       - 2 Project bullets (defaulting to Uber India & CDSCO Intel with bold headers)
  5. Preserves full-form ATS skill strings and re-orders dynamically based on JD match
  6. Injects into template.tex and compiles a strictly 1-page PDF via pdflatex
  7. Audits PDF page count and outputs summary

Usage:
    python triage_and_build.py <path_to_jd.txt> [--company "CompanyName"]
"""

import os
import sys
import json
import re
import subprocess
import argparse
import shutil
from pathlib import Path
from datetime import datetime

# Load .env file automatically
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

try:
    import pymupdf as fitz
except ImportError:
    try:
        import fitz
    except ImportError:
        fitz = None

try:
    from google import genai
except ImportError:
    genai = None

PREFERRED_MODELS = ["gemini-flash-lite-latest", "gemini-3.8-flash"]

from pydantic import BaseModel

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

ROOT             = Path(__file__).parent
MASTER_BANK_PATH   = ROOT / "master_bank.json"
TEMPLATE_PATH      = ROOT / "template.tex"
OUTPUT_DIR         = ROOT / "output"
OUTPUT_RESUMES_DIR = ROOT / "output_resumes"
MIKTEX_BIN         = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "MiKTeX" / "miktex" / "bin" / "x64"

FIT_SCORE_THRESHOLD = 75

# Fixed bullet counts per section (strictly 1-page geometry)
N_IQVIA    = 8
N_BAIN     = 3
N_JLL      = 3   # all 3, ranked
N_PROJECTS = 2   # top 2, defaulting to Uber India & CDSCO Intel

# Template placeholders
PLACEHOLDER_IQVIA      = "%DYNAMIC_IQVIA_BULLETS%"
PLACEHOLDER_BAIN       = "%DYNAMIC_BAIN_BULLETS%"
PLACEHOLDER_JLL        = "%DYNAMIC_JLL_BULLETS%"
PLACEHOLDER_PROJECTS   = "%DYNAMIC_PROJECT_BULLETS%"
PLACEHOLDER_SKILLS_BULLETS = "%DYNAMIC_SKILLS_BULLETS%"
PLACEHOLDER_SKILLS_P       = "%DYNAMIC_SKILLS_PRODUCT%"
PLACEHOLDER_SKILLS_T       = "%DYNAMIC_SKILLS_TECHNICAL%"
PLACEHOLDER_SKILLS_B       = "%DYNAMIC_SKILLS_BUSINESS%"
PLACEHOLDER_LEADERSHIP     = "%DYNAMIC_LEADERSHIP%"


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — Load master bank
# ─────────────────────────────────────────────────────────────────────────────

def load_master_bank(path: Path) -> tuple[dict, dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    index = {}
    for section_key in ("iqvia_bullets", "bain_bullets", "jll_bullets", "projects"):
        for item in data.get(section_key, []):
            index[item["id"]] = item

    print(f"[bank]  Loaded master bank ({len(index)} total bullet entries across sections).")
    return data, index


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — Role Scoring Rubric (Gemini LLM + Deterministic Fallback)
# ─────────────────────────────────────────────────────────────────────────────

TARGET_ROLE_SIGNALS = [
    "apm", "associate product manager", "pm-1", "pm1", "product manager",
    "product owner", "digital product manager", "technical product manager",
    "technical pm", "tpm", "growth product manager", "growth pm",
    "product operations manager", "product ops manager", "product ops",
    "senior product analyst", "product analyst", "product strategy manager",
    "product strategy", "founder's office", "founders office", "founder office",
    "0-3", "0 to 3", "1-3 years", "1-2 years", "0-2 years", "0-4 years",
    "entry level", "new grad", "early career"
]

PMM_GROWTH_SIGNALS = [
    "product marketing manager", "pmm", "product growth", "growth product",
    "growth product manager", "growth pm"
]
PMM_B2B_VALIDATORS = ["b2b", "enterprise", "gtm", "go-to-market", "positioning", "competitive intelligence", "user research"]

CONSUMER_PERF_MARKETING = [
    "paid ads", "paid ad", "influencer ops", "influencer marketing", "media buying",
    "meta ads", "google ads manager", "performance marketing", "paid acquisition", "paid media"
]

SENIOR_ROLES_REJECT = [
    "5+ years", "5-7 years", "6+ years", "7+ years", "8+ years", "10+ years",
    "principal product manager", "principal pm", "group product manager", "gpm",
    "director of product", "vp of product", "vice president of product", "chief product officer", "cpo",
    "field sales", "bdr", "sdr", "account executive", "territory sales manager", "pure sales"
]

DOMAIN_PREMIUM = [
    "ai", "artificial intelligence", "ml", "machine learning", "data",
    "b2b saas", "saas", "healthtech", "health tech", "consumer tech",
    "fintech", "analytics platform", "data platform", "llm", "generative ai"
]
DOMAIN_STANDARD = [
    "e-commerce", "edtech", "logtech", "proptech", "gaming",
    "digital product", "product manager"
]

TAG_ALIASES = {
    "sql":              "sql",
    "power bi":         "power_bi",
    "tableau":          "analytics",
    "product requirement": "prd",
    "prd":              "prd",
    "0 to 1":           "0_to_1",
    "0-to-1":           "0_to_1",
    "mvp":              "mvp",
    "user research":    "user_research",
    "user stories":     "user_stories",
    "go-to-market":     "gtm",
    "gtm":              "gtm",
    "analytics":        "analytics",
    "llm":              "llm",
    "generative ai":    "genai",
    "genai":            "genai",
    "roadmap":          "roadmap",
    "market sizing":    "market_sizing",
    "segmentation":     "segmentation",
    "competitive":      "competitive_intelligence",
    "dashboard":        "dashboard",
    "b2b":              "b2b",
    "saas":             "b2b",
    "stakeholder":      "stakeholder_management",
    "python":           "python",
    "automation":       "automation",
    "benchmarking":     "benchmarking",
    "natural language": "natural_language",
    "healthcare":       "healthcare",
    "data-driven":      "data_driven",
    "kpi":              "kpi",
}


def call_gemini_score(jd_text: str) -> dict | None:
    """
    Evaluates role fit using Gemini against the Sprint 3 Rubric:
      - Full 30/30 points for Target Archetypes: APM, PM-1, Product Owner, Digital PM,
        Senior Product Analyst, Product Analyst, Product Strategy Manager, Founder's Office,
        or B2B/Enterprise PMM / Product Growth (0–3 YoE or 0–4 YoE with consulting/analytics).
      - Explicit Rejections (< 50 Score):
        * Consumer performance-marketing PMM roles (paid ads, influencer ops, media buying).
        * Senior/Principal/Group PM roles strictly requiring 5+ years of PM experience.
        * Pure B2B field sales roles.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key or not genai:
        return None

    prompt = f"""
You are an expert technical talent evaluator for Digital Product Management roles.
Evaluate the following Job Description (JD) against candidate Jaivik Chauhan.

Candidate Profile:
- Current: Associate Consultant at IQVIA (Management Consulting + 0-to-1 Product Development).
- Prior: Summer Associate at Bain & Company (Tech & Cloud CoE), SDE Intern at JLL Technologies.
- Education: MBA from IIM Indore (2025, 81.9%), B.Tech in IT from VIT Vellore (2023, 91.9%).
- Experience level: 0–3 YoE, or 0–4 YoE when consulting/analytics counts.
- Verified Competency Bank: SQL, Python, Power BI, Tableau, PRD authoring, 0-to-1 MVP scoping, user stories, user research, GTM launch, KPI framework, market sizing, AI/LLM/RAG product design.

Scoring Rubric:
1. yoe_and_role_score (0 to 30 points):
   - Full 30/30 points for Target Role Archetypes (0–3 YoE, or 0–4 YoE when consulting/analytics counts):
     * Associate Product Manager (APM), Product Manager (PM-1), Product Owner, Digital Product Manager
     * Technical Product Manager (TPM), Growth Product Manager, Product Operations Manager
     * Senior Product Analyst, Product Analyst, Product Strategy Manager, Founder's Office
     * Product Marketing Manager (PMM) / Product Growth — ONLY when focused on B2B/enterprise positioning, GTM launch strategy, competitive intelligence, or user research.
   - Partial 10 to 15 points for general product roles requiring 3–4 YoE.
   - STRICT REJECTION (0 points, total score strictly < 50, verdict "REJECT"):
     * Consumer performance-marketing PMM roles focused purely on paid ad campaigns, influencer ops, or media buying.
     * Senior/Principal/Group PM roles strictly requiring 5+ years of prior PM experience.
     * Pure B2B field sales, BDR, or account executive roles.

2. competency_overlap_score (0 to 50 points):
   - Award 10 points for each of the top 5 core JD requirements mapping directly to verified competencies:
     (SQL, PRD, 0-to-1 MVP, User Research / User Stories, GTM Strategy, Analytics / BI Dashboards, Generative AI / LLM, Roadmap Planning, Market Sizing / Competitor Benchmarking, Stakeholder Management). Max 50 points.

3. domain_bridge_score (0 to 20 points):
   - 20 points: AI / Data PM, B2B SaaS, HealthTech, Consumer Tech, Analytics Platforms.
   - 10 points: General digital PM, FinTech, E-Commerce, EdTech, LogTech.
   - 5 points: Other non-tech domain.

Job Description:
\"\"\"{jd_text[:3500]}\"\"\"

Return ONLY a valid JSON object matching this schema:
{{
  "yoe_and_role_score": <int 0-30>,
  "competency_overlap_score": <int 0-50>,
  "domain_bridge_score": <int 0-20>,
  "total_score": <int 0-100>,
  "verdict": "PROCEED" | "LOW_FIT" | "REJECT",
  "matched_competencies": ["comp1", "comp2", ...],
  "yoe_note": "Short rationale",
  "domain_note": "Short rationale"
}}
"""
    client = genai.Client(api_key=api_key)
    for model_name in PREFERRED_MODELS:
        try:
            resp = client.models.generate_content(model=model_name, contents=prompt)
            if resp and resp.text:
                cleaned = re.sub(r"^```json\s*", "", resp.text.strip(), flags=re.MULTILINE)
                cleaned = re.sub(r"```$", "", cleaned.strip(), flags=re.MULTILINE)
                data = json.loads(cleaned)
                if "total_score" in data and "yoe_and_role_score" in data:
                    return data
        except Exception:
            continue
    return None


def compute_fit_score(jd_text: str, bank_data: dict) -> tuple[int, dict]:
    # 1. Try Gemini evaluation
    gemini_eval = call_gemini_score(jd_text)
    if gemini_eval:
        total = int(gemini_eval.get("total_score", 0))
        verdict = gemini_eval.get("verdict", "LOW_FIT")
        if verdict == "REJECT" or total < 50:
            total = min(total, 45) if verdict == "REJECT" else total
            verdict = "REJECT"
        elif total >= FIT_SCORE_THRESHOLD:
            verdict = "PROCEED"
        else:
            verdict = "LOW_FIT"

        breakdown = {
            "yoe_and_role_score": int(gemini_eval.get("yoe_and_role_score", 0)),
            "competency_overlap_score": int(gemini_eval.get("competency_overlap_score", 0)),
            "domain_bridge_score": int(gemini_eval.get("domain_bridge_score", 0)),
            "total": total,
            "verdict": verdict,
            "matched_competencies": gemini_eval.get("matched_competencies", []),
            "yoe_note": gemini_eval.get("yoe_note", ""),
            "domain_note": gemini_eval.get("domain_note", ""),
            "source": "gemini"
        }
        return total, breakdown

    # 2. Deterministic Fallback Engine
    jd_lower = jd_text.lower()
    breakdown = {"source": "deterministic"}

    # Explicit Rejections (< 50 Score)
    if any(sig in jd_lower for sig in CONSUMER_PERF_MARKETING) and not any(v in jd_lower for v in ["b2b", "enterprise", "prd", "0 to 1"]):
        breakdown["yoe_and_role_score"] = 0
        breakdown["competency_overlap_score"] = 0
        breakdown["domain_bridge_score"] = 0
        breakdown["total"] = 20
        breakdown["verdict"] = "REJECT"
        breakdown["yoe_note"] = "Auto-reject: Consumer performance-marketing PMM role (< 50)."
        return 20, breakdown

    if any(sig in jd_lower for sig in SENIOR_ROLES_REJECT):
        breakdown["yoe_and_role_score"] = 0
        breakdown["competency_overlap_score"] = 0
        breakdown["domain_bridge_score"] = 0
        breakdown["total"] = 0
        breakdown["verdict"] = "REJECT"
        breakdown["yoe_note"] = "Auto-reject: Senior/Principal/5+ YoE or sales role detected."
        return 0, breakdown

    # Check PMM / Growth rules
    is_pmm_growth = any(sig in jd_lower for sig in PMM_GROWTH_SIGNALS)
    if is_pmm_growth:
        if any(val in jd_lower for val in PMM_B2B_VALIDATORS):
            yoe_score = 30
            breakdown["yoe_note"] = "Target B2B/Enterprise Product Marketing/Growth role (+30)"
        else:
            breakdown["yoe_and_role_score"] = 0
            breakdown["total"] = 35
            breakdown["verdict"] = "REJECT"
            breakdown["yoe_note"] = "Auto-reject: Non-B2B/enterprise PMM role (< 50)."
            return 35, breakdown
    elif any(sig in jd_lower for sig in TARGET_ROLE_SIGNALS):
        yoe_score = 30
        breakdown["yoe_note"] = "Target APM/PM-1/Product Owner/Analyst/Founder's Office (+30)"
    else:
        yoe_score = 20
        breakdown["yoe_note"] = "General digital product role (+20)"
    breakdown["yoe_and_role_score"] = yoe_score

    # Competency Overlap Score (0–50)
    all_bullets = []
    for section_key in ("iqvia_bullets", "bain_bullets", "jll_bullets", "projects"):
        all_bullets.extend(bank_data.get(section_key, []))

    all_bank_tags = set()
    for b in all_bullets:
        all_bank_tags.update(b.get("tags", []))

    matched_competencies = []
    for jd_phrase, bank_tag in TAG_ALIASES.items():
        if jd_phrase in jd_lower and bank_tag in all_bank_tags:
            matched_competencies.append(jd_phrase)

    top5 = matched_competencies[:5]
    competency_score = len(top5) * 10
    breakdown["competency_overlap_score"] = competency_score
    breakdown["matched_competencies"] = top5

    # Domain Bridge Score (0–20)
    if any(d in jd_lower for d in DOMAIN_PREMIUM):
        domain_score = 20
        breakdown["domain_bridge_score"] = 20
        breakdown["domain_note"] = "AI/Data PM, B2B SaaS, HealthTech, Consumer Tech (+20)"
    elif any(d in jd_lower for d in DOMAIN_STANDARD):
        domain_score = 10
        breakdown["domain_bridge_score"] = 10
        breakdown["domain_note"] = "General digital PM domain (+10)"
    else:
        domain_score = 5
        breakdown["domain_bridge_score"] = 5
        breakdown["domain_note"] = "Other domain (+5)"

    total = yoe_score + competency_score + domain_score
    breakdown["total"] = total
    breakdown["verdict"] = "PROCEED" if total >= FIT_SCORE_THRESHOLD else "LOW_FIT"
    return total, breakdown
    breakdown["verdict"] = "PROCEED" if total >= FIT_SCORE_THRESHOLD else "LOW_FIT"
    return total, breakdown


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — Semantic Ranking Engine & Strict Slot-Ranking Logic
# ─────────────────────────────────────────────────────────────────────────────

def extract_jd_keywords(jd_text: str) -> list[str]:
    common_terms = [
        "product management", "analytics", "sql", "dashboard", "power bi",
        "tableau", "llm", "generative ai", "genai", "0 to 1", "0-to-1", "mvp",
        "prd", "user stories", "b2b", "saas", "user research", "growth",
        "gtm", "strategy", "roadmap", "prototyping", "stakeholder management",
        "natural language", "excel", "workflow", "automation"
    ]
    jd_lower = jd_text.lower()
    return [t for t in common_terms if t in jd_lower]


def rank_iqvia_slots(iqvia_pool: list[dict], jd_text: str) -> list[str]:
    """
    Enforces strict Top-3 Slot Ranking for IQVIA:
      - Slot 1 (Pinned Anchor): Always 'iqvia_0to1_mapping'
      - Slots 2 & 3 (High-Signal Hook): 2 bullets with highest semantic overlap with JD archetype
      - Slots 4 to 8 (Impact): Top remaining commercial/growth bullets
    """
    jd_keywords = extract_jd_keywords(jd_text)

    # Slot 1: Pinned Anchor
    slot_1_id = "iqvia_0to1_mapping"

    # Pool of remaining candidates
    remaining = [b for b in iqvia_pool if b["id"] != slot_1_id]

    # Archetype score for Slots 2 & 3
    def archetype_score(bullet):
        tags = bullet.get("tags", [])
        overlap = sum(1 for t in tags if any(kw in t.lower() or t.lower() in kw for kw in jd_keywords))
        # Extra boost for technical/analytics/AI/SaaS tags matching tech archetype
        tech_tags = {"ai", "llm", "genai", "analytics", "sql", "power_bi", "dashboard", "technical", "saas"}
        tech_overlap = len(set(tags).intersection(tech_tags))
        return overlap * 10 + tech_overlap * 5 + bullet.get("rank_weight", 5)

    sorted_for_hook = sorted(remaining, key=archetype_score, reverse=True)
    slot_2_3_bullets = sorted_for_hook[:2]
    slot_2_3_ids = [b["id"] for b in slot_2_3_bullets]

    # Pool for Slots 4 to 8 (all remaining bullets)
    selected_so_far = {slot_1_id} | set(slot_2_3_ids)
    rest_pool = [b for b in iqvia_pool if b["id"] not in selected_so_far]

    # Rank rest by overall business impact + tag match
    def impact_score(bullet):
        tags = bullet.get("tags", [])
        overlap = sum(1 for t in tags if any(kw in t.lower() or t.lower() in kw for kw in jd_keywords))
        return bullet.get("rank_weight", 5) * 10 + overlap * 5

    sorted_rest = sorted(rest_pool, key=impact_score, reverse=True)
    slot_4_8_ids = [b["id"] for b in sorted_rest[:5]]

    return [slot_1_id] + slot_2_3_ids + slot_4_8_ids


def deterministic_tag_rank(bullets: list[dict], jd_keywords: list[str]) -> list[str]:
    def score_bullet(item):
        tags = item.get("tags", [])
        overlap = sum(1 for t in tags if any(kw in t.lower() or t.lower() in kw for kw in jd_keywords))
        return overlap * 10 + item.get("rank_weight", item.get("default_rank", 5))

    sorted_items = sorted(bullets, key=score_bullet, reverse=True)
    return [b["id"] for b in sorted_items]


def rank_achievements(jd_text: str, bank_data: dict, api_key: str | None) -> dict[str, list[str]]:
    jd_keywords = extract_jd_keywords(jd_text)

    # 1. IQVIA with strict slot ranking
    iqvia_ranked = rank_iqvia_slots(bank_data["iqvia_bullets"], jd_text)

    # 2. Bain ranking
    bain_ranked = deterministic_tag_rank(bank_data["bain_bullets"], jd_keywords)

    # 3. JLL ranking
    jll_ranked = deterministic_tag_rank(bank_data["jll_bullets"], jd_keywords)

    # 4. Project ranking (default: Uber India & CDSCO Intel)
    proj_ranked = ["project_uber_prd", "project_cdsco_intel", "project_smart_speaker", "project_self_watering_pot"]

    return {
        "iqvia": iqvia_ranked,
        "bain": bain_ranked,
        "jll": jll_ranked,
        "projects": proj_ranked,
        "jd_keywords": jd_keywords
    }


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — Select bullets from ranked pools
# ─────────────────────────────────────────────────────────────────────────────

def select_bullets(ranking: dict[str, list[str]], bank_index: dict) -> dict[str, list]:
    def resolve(ranked_ids: list[str], cap: int) -> list[str]:
        texts = []
        for aid in ranked_ids[:cap]:
            if aid in bank_index:
                texts.append(bank_index[aid]["text"])
        return texts

    def resolve_projects(ranked_ids: list[str], cap: int) -> list[dict]:
        projs = []
        for aid in ranked_ids[:cap]:
            if aid in bank_index:
                projs.append(bank_index[aid])
        return projs

    return {
        "iqvia":    resolve(ranking["iqvia"],    N_IQVIA),
        "bain":     resolve(ranking["bain"],     N_BAIN),
        "jll":      resolve(ranking["jll"],      N_JLL),
        "projects": resolve_projects(ranking["projects"], N_PROJECTS),
    }


# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — Reorder skills (Preserving exact full-form ATS strings)
# ─────────────────────────────────────────────────────────────────────────────

def reorder_skills(bank_data: dict, jd_keywords: list[str]) -> dict[str, list[str]]:
    kw_lower = [k.lower() for k in jd_keywords]

    # Keyword mappings to full strings
    ALIAS_MAP = {
        "sql": ["sql"],
        "python": ["python"],
        "power bi": ["power bi"],
        "tableau": ["tableau"],
        "llm": ["large language model (llm) apis"],
        "generative ai": ["generative ai"],
        "genai": ["generative ai"],
        "prd": ["product requirement documents (prds)"],
        "user stories": ["user stories"],
        "analytics": ["product analytics", "data analytics"],
        "gtm": ["go-to-market (gtm) strategy"],
        "strategy": ["go-to-market (gtm) strategy", "pharmaceutical and life sciences commercial strategy"],
        "user research": ["user research"],
        "mvp": ["mvp scoping"],
        "roadmap": ["roadmap prioritization"],
        "segmentation": ["customer segmentation"],
        "market sizing": ["market sizing"],
        "market research": ["market research"],
        "competitive": ["competitive intelligence"],
        "stakeholder": ["stakeholder management"],
        "rest apis": ["rest apis"],
        "api": ["rest apis", "large language model (llm) apis"],
    }

    def sort_skill_list(skills: list[str]) -> list[str]:
        def score(skill):
            s_lower = skill.lower()
            pts = 0
            # Direct keyword match
            for kw in kw_lower:
                if kw in s_lower:
                    pts += 10
                for alias_kw, matched_skills in ALIAS_MAP.items():
                    if alias_kw in kw_lower and any(m in s_lower for m in matched_skills):
                        pts += 15
            return -pts
        return sorted(skills, key=score)

    raw_skills = bank_data.get("skills", {})
    return {
        "product":   sort_skill_list(raw_skills.get("product", [])),
        "technical": sort_skill_list(raw_skills.get("technical", [])),
        "business":  sort_skill_list(raw_skills.get("business", [])),
    }


# ─────────────────────────────────────────────────────────────────────────────
# STEP 6 — LaTeX formatting and Injection
# ─────────────────────────────────────────────────────────────────────────────

def sanitize_latex(text: str) -> str:
    # 1. Strip any pre-escaped backslashes so nothing double-escapes into \$
    text = re.sub(r'\\+\$', '$', text)
    text = re.sub(r'\\+%', '%', text)
    text = re.sub(r'\\+&', '&', text)
    text = text.replace('$A/B$', 'A/B')
    text = text.replace('₹', 'Rs. ')
    # 2. Escape LaTeX special characters
    text = text.replace('&', r'\&')
    text = text.replace('%', r'\%')
    text = text.replace('$', r'\$')
    text = text.replace('#', r'\#')
    text = text.replace('_', r'\_')
    text = text.replace('<', r'\textless{}')
    text = text.replace('>', r'\textgreater{}')
    text = text.replace('~', r'$\sim$')
    # 3. Convert **bold** markdown markers to \textbf{...} AFTER escaping
    text = re.sub(r'\*\*(.+?)\*\*', r'\\textbf{\1}', text)
    return text


escape_latex = sanitize_latex


def build_items(texts: list[str]) -> str:
    if not texts:
        return "    % No bullets selected"
    return "\n".join(f"    \\item {escape_latex(t)}" for t in texts)


def build_project_items(projects: list) -> str:
    """
    Formats project bullets with bold titles and clickable link if present:
      \\item \\textbf{Title} (\\href{url}{\\underline{link_label}}): Description
    """
    items = []
    for p in projects:
        if isinstance(p, dict):
            text = p.get("text", "")
            url = p.get("url")
            link_label = p.get("link_label")
        else:
            text = str(p)
            url = None
            link_label = None

        if ":" in text:
            title, rest = text.split(":", 1)
            sanitized_title = sanitize_latex(title.strip())
            sanitized_rest = sanitize_latex(rest.strip())
            if url and link_label:
                items.append(
                    f"    \\item \\textbf{{{sanitized_title}}} "
                    f"(\\href{{{url}}}{{\\underline{{{sanitize_latex(link_label)}}}}}): "
                    f"{sanitized_rest}"
                )
            else:
                items.append(f"    \\item \\textbf{{{sanitized_title}:}} {sanitized_rest}")
        else:
            items.append(f"    \\item {sanitize_latex(text)}")
    return "\n".join(items)


def build_skills_bullets(skills: dict) -> str:
    """Formats skills as itemize bullets: \\item \\textbf{Product:} ..."""
    lines = [
        f"    \\item \\textbf{{Product:}} {sanitize_latex(', '.join(skills['product']))}",
        f"    \\item \\textbf{{Technical:}} {sanitize_latex(', '.join(skills['technical']))}",
        f"    \\item \\textbf{{Business:}} {sanitize_latex(', '.join(skills['business']))}",
    ]
    return "\n".join(lines)


def build_leadership_items(items: list[dict]) -> str:
    """Formats leadership bullets with bold labels: \\item \\textbf{Label:} Description"""
    lines = []
    for item in items:
        t = item.get("text", "")
        if ":" in t:
            label, desc = t.split(":", 1)
            lines.append(f"    \\item \\textbf{{{escape_latex(label.strip())}:}} {escape_latex(desc.strip())}")
        else:
            lines.append(f"    \\item {escape_latex(t)}")
    return "\n".join(lines)


def inject_all(template_tex: str, bullets: dict, skills: dict, bank_data: dict) -> str:
    tex = template_tex

    tex = tex.replace(PLACEHOLDER_IQVIA,          build_items(bullets["iqvia"]))
    tex = tex.replace(PLACEHOLDER_BAIN,           build_items(bullets["bain"]))
    tex = tex.replace(PLACEHOLDER_JLL,            build_items(bullets["jll"]))
    tex = tex.replace(PLACEHOLDER_PROJECTS,       build_project_items(bullets["projects"]))
    tex = tex.replace(PLACEHOLDER_SKILLS_BULLETS, build_skills_bullets(skills))

    # Backward compatibility with inline skills and dynamic leadership placeholders
    tex = tex.replace(PLACEHOLDER_SKILLS_P, escape_latex(", ".join(skills["product"])))
    tex = tex.replace(PLACEHOLDER_SKILLS_T, escape_latex(", ".join(skills["technical"])))
    tex = tex.replace(PLACEHOLDER_SKILLS_B, escape_latex(", ".join(skills["business"])))

    leadership_items = bank_data.get("leadership", [])
    tex = tex.replace(PLACEHOLDER_LEADERSHIP, build_leadership_items(leadership_items))

    return tex


# ─────────────────────────────────────────────────────────────────────────────
# STEP 7 — PDF Compilation & Verification
# ─────────────────────────────────────────────────────────────────────────────

def ensure_miktex_on_path():
    if shutil.which("pdflatex"):
        return
    if MIKTEX_BIN.exists():
        os.environ["PATH"] = str(MIKTEX_BIN) + os.pathsep + os.environ.get("PATH", "")


def compile_pdf(tex_content: str, company_name: str, role_title: str = "") -> Path:
    OUTPUT_RESUMES_DIR.mkdir(parents=True, exist_ok=True)
    ensure_miktex_on_path()

    safe_co  = re.sub(r"[^\w]", "", company_name.replace(" ", ""))
    if not safe_co:
        safe_co = "Company"

    pdf_name = f"Jaivik_Chauhan_PM_{safe_co}.pdf"
    tex_name = f"Jaivik_Chauhan_PM_{safe_co}.tex"
    tex_path = OUTPUT_RESUMES_DIR / tex_name

    with open(tex_path, "w", encoding="utf-8") as f:
        f.write(tex_content)
    print(f"[latex] Written populated TeX: {tex_path.name}")

    pdflatex_cmd = shutil.which("pdflatex") or str(MIKTEX_BIN / "pdflatex.exe")

    for pass_num in range(1, 3):
        print(f"[latex] pdflatex pass {pass_num}/2 ...")
        cmd = [
            pdflatex_cmd,
            "-halt-on-error",
            "-interaction=nonstopmode",
            f"-output-directory={str(OUTPUT_RESUMES_DIR)}",
            str(tex_path)
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            log_path = OUTPUT_RESUMES_DIR / tex_path.with_suffix(".log").name
            snippet = r.stdout[-2000:] if r.stdout else r.stderr[-2000:]
            raise RuntimeError(f"pdflatex failed (exit code {r.returncode}).\nLog: {log_path}\n{snippet}")

    pdf_path = OUTPUT_RESUMES_DIR / pdf_name
    if not pdf_path.exists():
        raise RuntimeError(f"pdflatex completed but PDF not found: {pdf_path}")

    # Clean up auxiliary LaTeX build files (retaining .tex alongside .pdf for inspection)
    for ext in (".aux", ".log", ".out"):
        aux_file = OUTPUT_RESUMES_DIR / f"{tex_path.stem}{ext}"
        if aux_file.exists():
            try:
                aux_file.unlink()
            except OSError:
                pass

    # Check page count
    if fitz:
        try:
            doc = fitz.open(pdf_path)
            num_pages = len(doc)
            print(f"[verify] PDF compiled successfully: {num_pages} page(s).")
            if num_pages == 1:
                print("[verify] ✅ Geometry check passed: STRICTLY 1-PAGE PDF.")
            else:
                print(f"[warn]   PDF is {num_pages} pages! Please adjust spacing.")
        except Exception as e:
            print(f"[verify] Could not inspect PDF page count via fitz: {e}")

    return pdf_path


def build_resume_for_jd(jd_text: str, company_name: str = "TargetCompany", role_title: str = "") -> dict:
    """
    Modular execution function for pipeline integration:
      - Computes deterministic fit score
      - If fit_score >= 75: ranks achievements, selects bullets, compiles 1-page PDF
      - Returns structured dict with fit_score, breakdown, pdf_path, selected_bullets
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    bank_data, bank_index = load_master_bank(MASTER_BANK_PATH)
    fit_score, breakdown = compute_fit_score(jd_text, bank_data)

    result = {
        "company": company_name,
        "role": role_title,
        "fit_score": fit_score,
        "breakdown": breakdown,
        "verdict": breakdown.get("verdict", "LOW_FIT"),
        "pdf_path": None,
        "selected_bullets": None,
        "ranking": None,
    }

    if breakdown.get("verdict") == "REJECT" or fit_score < FIT_SCORE_THRESHOLD:
        return result

    ranking = rank_achievements(jd_text, bank_data, api_key)
    bullets = select_bullets(ranking, bank_index)
    skills = reorder_skills(bank_data, ranking["jd_keywords"])

    template_tex = TEMPLATE_PATH.read_text(encoding="utf-8")
    populated_tex = inject_all(template_tex, bullets, skills, bank_data)

    pdf_path = compile_pdf(populated_tex, company_name, role_title)
    result["pdf_path"] = pdf_path
    result["selected_bullets"] = bullets
    result["ranking"] = ranking
    return result


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="PM Career Copilot — Resume Builder v2.2")
    parser.add_argument("jd_file",   help="Path to job description .txt file")
    parser.add_argument("--company", default="TargetCompany", help="Company name for PDF filename")
    parser.add_argument("--role",    default="",              help="Role title for PDF filename")
    args = parser.parse_args()

    jd_path = Path(args.jd_file)
    if not jd_path.exists():
        sys.exit(f"[error] JD file not found: {jd_path}")
    jd_text = jd_path.read_text(encoding="utf-8")
    print(f"[jd]    Loaded JD: {jd_path.name} ({len(jd_text)} characters)")

    res = build_resume_for_jd(jd_text, args.company, args.role)

    print(f"\n{'─'*55}")
    print(f"  FIT SCORE BREAKDOWN")
    print(f"{'─'*55}")
    breakdown = res["breakdown"]
    print(f"  YoE & Role Score    : {breakdown['yoe_and_role_score']:>3}/30  — {breakdown.get('yoe_note','')}")
    print(f"  Competency Overlap  : {breakdown['competency_overlap_score']:>3}/50  — {breakdown.get('matched_competencies', [])}")
    print(f"  Domain Bridge Score : {breakdown['domain_bridge_score']:>3}/20  — {breakdown.get('domain_note','')}")
    print(f"{'─'*55}")
    print(f"  TOTAL FIT SCORE     : {res['fit_score']}/100  [{res['verdict']}]")
    print(f"{'─'*55}\n")

    if res["verdict"] == "REJECT":
        print("[gate]  Role rejected (5+ YoE or non-product). No PDF generated.")
        sys.exit(0)

    if res["fit_score"] < FIT_SCORE_THRESHOLD:
        print(f"[gate]  Fit score {res['fit_score']} < threshold {FIT_SCORE_THRESHOLD}. Skipping compilation.")
        sys.exit(0)

    bullets = res["selected_bullets"]
    ranking = res["ranking"]
    print(f"[select] Selected {len(bullets['iqvia'])} IQVIA bullets (strict slot-ranked):")
    print(f"         - Slot 1 (Anchor) : {ranking['iqvia'][0]}")
    print(f"         - Slots 2-3 (Hook): {ranking['iqvia'][1:3]}")
    print(f"         - Slots 4-8       : {ranking['iqvia'][3:8]}")
    print(f"[select] Selected {len(bullets['bain'])} Bain bullets (top-3): {ranking['bain'][:3]}")
    print(f"[select] Selected {len(bullets['jll'])} JLL bullets (all-3): {ranking['jll'][:3]}")
    print(f"[select] Selected {len(bullets['projects'])} Project bullets (top-2): {ranking['projects'][:2]}")

    print(f"\n{'='*60}")
    print(f"  🎉  BESPOKE 1-PAGE RESUME READY FOR HUMAN REVIEW!")
    print(f"  📄  PDF Path: {res['pdf_path']}")
    print(f"  📊  Sections: {len(bullets['iqvia'])} IQVIA | {len(bullets['bain'])} Bain | {len(bullets['jll'])} JLL | {len(bullets['projects'])} Projects")
    print(f"{'='*60}\n")
    print(f"  🎉  BESPOKE 1-PAGE RESUME READY FOR HUMAN REVIEW!")
    print(f"  📄  PDF Path: {pdf_path}")
    print(f"  📊  Sections: {len(bullets['iqvia'])} IQVIA | {len(bullets['bain'])} Bain | {len(bullets['jll'])} JLL | {len(bullets['projects'])} Projects")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
