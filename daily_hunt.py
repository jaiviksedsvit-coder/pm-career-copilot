"""
daily_hunt.py — PM Career Copilot: 1-Command Morning Execution Workflow
=======================================================================
Sprint 4: The Automated Daily HITL Ingestion, Triage & Staging Loop.

Single Master Entrypoint for your morning routine (≤ 15 mins daily budget):
  1. Ingests fresh postings from JSearch India (via RAPIDAPI_KEY, date_posted="3days"),
     local Dossier CRM database, and ./manual_jds/.
  2. Evaluates fit scores (Rule 5 rubric via Gemini LLM / tag engine).
  3. For roles scoring >= 75:
     - Compiles bespoke 1-page PDF: ./output_resumes/Jaivik_Chauhan_PM_<Company>.pdf
     - Discovers decision maker / alumni contact via 3-tier X-Ray + MX validation.
     - Stages hyper-personalized cold pitch in Gmail drafts (or DRY_RUN if credentials pending).
  4. Prints an executive Morning Action Summary with direct application links.
  5. If --open-urls is specified, launches all qualifying application links in the default browser.

Usage:
  python daily_hunt.py [--dry-run] [--open-urls] [--force] [--manual-only]
  python daily_hunt.py --stats
"""

import os
import sys
import argparse
import webbrowser
import sqlite3
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

STATE_DB_PATH = ROOT / "copilot_state.db"

from dossier_bridge import run_bridge, print_state_summary, init_state_db


def print_morning_action_summary(opened_urls: bool = False):
    """
    Queries copilot_state.db and prints a high-priority action summary for today's review.
    Optionally launches qualifying apply URLs in the user's default browser.
    """
    if not STATE_DB_PATH.exists():
        print("[hunt] State database not found.")
        return

    conn = init_state_db(STATE_DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT company, role, fit_score, contact_name, alum_tag, draft_status, url, pdf_path
        FROM processed_jobs
        WHERE fit_score >= 75
        ORDER BY fit_score DESC, created_at DESC
    """)
    rows = cur.fetchall()
    conn.close()

    if not rows:
        print("\n[hunt] ℹ️  No qualified roles (>= 75 fit score) ready for review today.")
        return

    print(f"\n{'='*110}")
    print(f"  🌅  MORNING PM HUNT ACTION SUMMARY — READY FOR HUMAN REVIEW ({len(rows)} Qualified Roles)")
    print(f"{'='*110}")
    print(f"{'Company':<20} {'Role':<26} {'Score':<7} {'Contact':<18} {'Status':<16} {'Application Link'}")
    print(f"{'-'*20} {'-'*26} {'-'*7} {'-'*18} {'-'*16} {'-'*30}")

    urls_to_open = []
    for r in rows:
        co = (r['company'][:18] + '..') if len(r['company']) > 18 else r['company']
        ro = (r['role'][:24] + '..') if len(r['role']) > 24 else r['role']
        score = f"{r['fit_score']}/100"
        contact = (r['contact_name'][:16] + '..') if r['contact_name'] and len(r['contact_name']) > 16 else (r['contact_name'] or "—")
        status = r['draft_status'] or "—"
        url = r['url'] or "—"

        disp_url = (url[:35] + '...') if len(url) > 35 else url
        print(f"{co:<20} {ro:<26} {score:<7} {contact:<18} {status:<16} {disp_url}")

        if opened_urls and url and url.startswith("http"):
            urls_to_open.append((r['company'], url))

    print(f"{'='*110}")

    if urls_to_open:
        print(f"\n[browser] 🌐 Opening {len(urls_to_open)} qualifying application links in default browser...")
        for comp, link in urls_to_open:
            print(f"          Opening: {comp} -> {link[:65]}...")
            webbrowser.open(link)

    print("\n[hitl] 📌 15-Minute Morning Protocol:")
    print("       1. Review staged drafts in Gmail: check contact & proof point.")
    print("       2. Review generated 1-page PDF in ./output_resumes/.")
    print("       3. Submit job application online and hit Send on the email draft.")
    print("       4. Update status in dashboard: streamlit run dashboard.py\n")


def main():
    parser = argparse.ArgumentParser(description="PM Career Copilot — Daily Morning Runner")
    parser.add_argument("--dry-run", action="store_true", help="Run pipeline in dry-run mode (no live Gmail API drafts)")
    parser.add_argument("--open-urls", action="store_true", help="Automatically open qualified job application URLs in browser")
    parser.add_argument("--force", action="store_true", help="Re-process jobs even if already in state DB")
    parser.add_argument("--manual-only", action="store_true", help="Scan only ./manual_jds/ and ignore JSearch/Dossier/Gmail")
    parser.add_argument("--skip-gmail", action="store_true", help="Skip Gmail inbox alert ingestion")
    parser.add_argument("--stats", action="store_true", help="Show database audit trail and exit")
    args = parser.parse_args()

    if args.stats:
        print_state_summary()
        return

    print("\n" + "="*80)
    print("  ☕  PM CAREER COPILOT — MORNING HUNT INITIATED")
    print(f"      Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Target: APM / PM-1 India")
    print("="*80 + "\n")

    # 1. Ingest Inbound Job Alerts & Leads from Gmail (Sprint 5)
    if not args.manual_only and not args.skip_gmail:
        try:
            from gmail_ingestor import run_gmail_ingestion
            run_gmail_ingestion(dry_run=args.dry_run, force=args.force)
        except Exception as e:
            print(f"[hunt] ⚠️ Gmail alert ingestion notice: {e}")

    # 2. Run the Multi-Source Ingestion & Triage Bridge (JSearch India + Dossier + manual_jds)
    run_bridge(dry_run=args.dry_run, manual_only=args.manual_only, force=args.force)

    # 3. Print Action Summary & Open URLs
    print_morning_action_summary(opened_urls=args.open_urls)


if __name__ == "__main__":
    main()
