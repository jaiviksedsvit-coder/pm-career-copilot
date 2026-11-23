# PM Career Copilot — Agent Rules & Project Context

> This file is the source of truth for every AI agent working in this repository.
> Read this before writing any code, generating any content, or making any architectural decision.

---

## 1. Candidate Persona

| Field | Detail |
|---|---|
| **Name** | Jaivik Chauhan |
| **Location** | Mumbai, India |
| **Target Roles** | APM, PM-1 (Digital Product Management) |
| **Primary Email** | `jaivik.chauhan.2026@gmail.com` (Outreach sender, draft staging, portal alerts: Naukri, Indeed) |
| **Secondary Email** | `jaivik.sedsvit@gmail.com` (Read-only alert ingestion: LinkedIn, campus) |
| **Background** | Management Consulting (IQVIA Associate Consultant, Bain Summer Associate) + Engineering (VIT B.Tech IT, JLL SDE Intern) |
| **Education** | MBA — IIM Indore (2025); B.Tech IT — VIT Vellore |
| **Transition Goal** | Consulting & Engineering → Digital Product Management |
| **Daily Time Budget** | ≤ 15 minutes of human review per day |

---

## 2. The Problem (Why This Exists)

- Generic resumes are filtered out by ATS keyword scoring before a human ever sees them.
- Mass-applying via bots risks LinkedIn bans and produces low-quality, untracked spam.
- The solution is **hyper-personalization at scale**: one bespoke 1-page resume and one warm outreach email per targeted role, with a human making the final call on every send.

---

## 3. The Product (What We Are Building)

A **Human-in-the-Loop (HITL) Career Transition Engine** with two layers:

### Layer 1 — Discovery & Tracking
- **Tool:** Open-source [`AienTech/dossier`](https://github.com/AienTech/dossier) (cloned to `dossier_crm/`)
- **Function:** Aggregate job listings (via JSearch API) and track application status on a Kanban board.

### Layer 2 — Intelligence & Execution
Custom Python scripts that form a pipeline:
```
Dossier Job → JD Tag Extraction → master_bank.json Semantic Match
    → LaTeX Template Injection → pdflatex/tectonic Compile
    → Gmail API Draft Staging → Human Review & Send
```

---

## 4. Core Architecture

### Key Files & Directory Layout
| File / Folder | Purpose |
|---|---|
| `daily_hunt.py` | Single master runner for the daily morning review workflow |
| `dashboard.py` | Streamlit Command Center (interactive tracker, 1-click apply, resume download) |
| `gmail_ingestor.py` | Dual-inbox job alert parser (Naukri, Indeed, Wellfound, IIMJobs, LinkedIn) & Google resolver |
| `dossier_bridge.py` | JSearch India (4 tracks) & Dossier CRM pipeline coordinator |
| `outreach_drafter.py` | Decision-maker / Alumni discovery & warm pitch staging in Gmail (`Job Copilot` label) |
| `triage_and_build.py` | Deterministic Rule 5 scoring & LaTeX 1-page PDF compiler |
| `auth_gmail_server.py` | Local OAuth server for primary and secondary Google accounts |
| `master_bank.json` | The single source of truth for all verified resume achievements |
| `template.tex` | RAG injection LaTeX template with `%DYNAMIC_*_BULLETS%` placeholders |
| `output_resumes/` | Canonical directory for all compiled 1-page PDF resumes and `.tex` source files |
| `manual_jds/` | Drop folder for ad-hoc or offline job descriptions to evaluate |
| `Resume/` | Original master source resumes (`.docx`) |
| `tests/` | Unit and integration tests (`test_gmail_ingestor.py`) |
| `copilot_state.db` | SQLite database recording all evaluated jobs, scores, and draft statuses |

### Pipeline Placeholders in `template.tex`
| Placeholder | Maps To |
|---|---|
| `%DYNAMIC_IQVIA_BULLETS%` | Top 7 IQVIA achievements from `master_bank.json` |
| `%DYNAMIC_BAIN_BULLETS%` | Top 3 Bain achievements from `master_bank.json` |
| `%DYNAMIC_JLL_BULLETS%` | 3 JLL achievements from `master_bank.json` |
| `%DYNAMIC_PROJECT_BULLETS%` | Top 2 Projects (Uber India, CDSCO Intel default) |
| `%DYNAMIC_SKILLS_PRODUCT%` | JD-prioritized product skills |
| `%DYNAMIC_SKILLS_TECHNICAL%` | JD-prioritized technical skills |
| `%DYNAMIC_SKILLS_BUSINESS%` | JD-prioritized business skills |
| `%DYNAMIC_LEADERSHIP%` | Leadership & Competitions achievements |

---

## 5. The Guardrails (Non-Negotiable Rules)

### 🚫 Rule 1: Zero Hallucination — NEVER Invent Resume Content
- The AI acts **only as a semantic router**, not a content generator.
- Resume bullet points must **always** be selected verbatim from `master_bank.json`.
- **Never** use an LLM to write, rephrase, embellish, or invent achievement bullets.
- If no bullet in `master_bank.json` matches a JD requirement, surface that gap to the user — do not fill it with generated text.

### 📄 Rule 2: Strict LaTeX Compilation
- All resumes must compile cleanly via local `pdflatex` or `tectonic`.
- Geometry: **left=0.4in, right=0.4in, top=0.35in, bottom=0.35in, 9.5pt font, letter paper**.
- Output must be a **strict 1-page PDF**. No overflow permitted.
- Never modify the document class or geometry settings to fit content — instead, adjust spacing or bullet selection.

### 🛡️ Rule 3: Anti-Ban Architecture
- **Never** use Playwright, Selenium, Puppeteer, or any browser automation to log into LinkedIn, Indeed, or any job board directly.
- Job discovery happens through **Dossier + JSearch API** only.
- Contact discovery uses **Google X-Ray search** (manual or scripted query generation), not LinkedIn scraping.
- Outreach drafts are staged via the **Gmail API** (`drafts.create`). The user clicks **Send** manually.
- The user manually clicks **Apply** or **Submit** on every job application.

### 🔑 Rule 4: Human-in-the-Loop is Sacred
- Every pipeline output (resume PDF, email draft) is staged for review — never auto-sent or auto-submitted.
- The agent surfaces options and recommendations; the **human makes all final decisions**.
- The daily workflow must remain completable in **≤ 15 minutes** of human time.

### 🏷️ Rule 5: Deterministic Fit Scoring & Tag-Based Ranking
- `fit_score` (0–100) is calculated deterministically as:
  - `yoe_and_role_score` (0–30 pts): 30 pts for APM/PM-1/0–3 YoE; 15 pts for 3–4 YoE; 0 pts (auto-reject) for 5+ YoE or non-product roles.
  - `competency_overlap_score` (0–50 pts): 10 pts for each of the JD's top 5 core requirements mapping to bank tags.
  - `domain_bridge_score` (0–20 pts): 20 pts for AI/Data PM, B2B SaaS, HealthTech, Consumer Tech; 10 pts for general digital PM.
- Gate: Score must be ≥ 75 to generate a resume.
- Semantic ranking ranks pre-verified bullet pools and re-orders skill lists by relevance.

---

## 6. Verified Achievement Bank Pools

The canonical data lives in `master_bank.json` partitioned by section:
- `iqvia_bullets` (12 verified bullets; selects top 7)
- `bain_bullets` (5 verified bullets; selects top 3)
- `jll_bullets` (3 condensed bullets; ranks all 3)
- `projects` (4 verified projects: Uber India PRD, CDSCO Intel platform, Smart Speaker, Self-Watering Pot; selects top 2)
- `skills` (Product, Technical, Business lists dynamically sorted by JD match)
- `leadership` (National case competitions and campus leadership)

---

## 7. Sprint Cadence

| Sprint | Goal |
|---|---|
| **Sprint 1** ✅ | Repo scaffold: clone Dossier CRM, create `master_bank.json`, `template.tex` |
| **Sprint 2** ✅ | Build `triage_and_build.py`: Deterministic scoring + semantic ranking + LaTeX injection + 1-page PDF compile |
| **Sprint 3** ✅ | Build `outreach_drafter.py` + `dossier_bridge.py`: Dossier CRM & fallback ingestion, state DB deduplication, ban-safe contact hunting, MX verification, cold pitch drafting & Gmail API staging |
| **Sprint 4** ✅ | Daily Morning Runner (`daily_hunt.py`) & Interactive Dashboard (`dashboard.py`): Ingest JSearch India + Dossier + `manual_jds/`, triage, compile bespoke 1-page PDFs (`./output_resumes/`), stage Gmail drafts, `--open-urls` browser launcher, Streamlit analytics |
| **Sprint 5** ✅ | Gmail Alert & Recruiter Ingestor (`gmail_ingestor.py`): Ingest Gmail alerts (Wellfound, Naukri, IIMJobs, Cutshort, Instahyre, Indeed, LinkedIn) & recruiter emails (`newer_than:3d`), parse PDF/DOCX attachments, Gemini structured lead extraction with email link preservation, direct tracking link unfurling (HTTP redirects), origin-aware portal search queries & scoring, direct recruiter replies |

---

*Last updated: Sprint 5 Ingestor Enhancement Complete — 2026-11-18*
