"""
test_gmail_ingestor.py — Unit & Integration Test for Sprint 5 Gmail Ingestor
=============================================================================
Tests:
  1. Origin portal detection across Indian & global platforms (Wellfound, Naukri, IIMJobs, Cutshort, Instahyre, Indeed, LinkedIn)
  2. Origin-aware URL scoring & ATS weighting
  3. Direct tracking link unfurling and parameter cleaning
  4. Gemini lead extraction on multi-job portal alert and direct recruiter email
  5. Email deduplication & state DB tracking
"""

import sys
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from gmail_ingestor import (
    clean_tracking_params,
    score_apply_url,
    detect_email_portal,
    unfurl_redirect_url,
    PORTAL_CONFIGS,
    extract_leads_with_gemini,
    resolve_job_details,
    init_gmail_tables,
    is_email_parsed,
    mark_email_parsed,
    STATE_DB_PATH
)

def test_origin_portal_detection():
    print("[test 1] Testing Origin Portal Detection...")
    cases = [
        ("alerts@wellfound.com", "Wellfound Alert", "5 new jobs", "wellfound"),
        ("no-reply@angel.co", "AngelList Talent", "Weekly matches", "wellfound"),
        ("jobalerts@naukri.com", "Naukri Job Alerts", "Product Manager roles", "naukri"),
        ("daily@iimjobs.com", "IIMJobs Daily Digest", "Senior Product roles", "iimjobs"),
        ("notifications@cutshort.io", "Cutshort.io", "Founders looking for PM", "cutshort"),
        ("alerts@instahyre.com", "Instahyre Opportunities", "APM opening", "instahyre"),
        ("alert@indeed.com", "Indeed Job Alert", "Jobs in Bengaluru", "indeed"),
        ("jobalerts-noreply@linkedin.com", "LinkedIn Job Alert", "3 new jobs match", "linkedin"),
        ("hr@startup.com", "Priya Sharma", "Opportunity at CRED", "general"),
    ]

    for sender, from_hdr, subj, expected in cases:
        detected = detect_email_portal(sender, from_hdr, subj)
        assert detected == expected, f"Failed for {sender}: expected {expected}, got {detected}"

    print("         ✅ All portal origins correctly identified (Wellfound, Naukri, IIMJobs, Cutshort, Instahyre, Indeed, LinkedIn, General)!")


def test_origin_aware_scoring():
    print("\n[test 2] Testing Origin-Aware URL Scoring & ATS Weighting...")
    # Swiggy job on Greenhouse (ATS)
    ats_url = "https://boards.greenhouse.io/swiggy/jobs/12345"
    score_ats = score_apply_url(ats_url, "Swiggy", origin="general")
    assert score_ats >= 80, f"Expected ATS score >= 80, got {score_ats}"

    # Wellfound origin: wellfound link should receive origin boost
    wf_url = "https://wellfound.com/jobs/99999-product-manager"
    score_wf_origin = score_apply_url(wf_url, "Zepto", origin="wellfound")
    score_wf_non_origin = score_apply_url(wf_url, "Zepto", origin="general")
    assert score_wf_origin > score_wf_non_origin, f"Origin score ({score_wf_origin}) should exceed non-origin ({score_wf_non_origin})"
    assert score_wf_origin >= 100, f"Expected origin match score >= 100, got {score_wf_origin}"

    # IIMJobs origin: iimjobs link receives origin boost
    iim_url = "https://www.iimjobs.com/j/flipkart-apm-1234567.html"
    score_iim = score_apply_url(iim_url, "Flipkart", origin="iimjobs")
    assert score_iim >= 100

    # Modern ATS: AshbyHQ & Keka
    ashby_url = "https://jobs.ashbyhq.com/cred/product-lead"
    keka_url = "https://acme.keka.com/careers/jobdetails/999"
    assert score_apply_url(ashby_url, "Cred") >= 80
    assert score_apply_url(keka_url, "Acme") >= 80

    # Negative penalties: search aggregator / review
    blog_url = "https://example.com/search?q=zepto"
    assert score_apply_url(blog_url, "Zepto") < 0

    print(f"         ATS Score (Greenhouse): {score_ats}")
    print(f"         Origin Score (Wellfound for Wellfound Alert): {score_wf_origin}")
    print(f"         Origin Score (IIMJobs for IIMJobs Alert): {score_iim}")
    print(f"         Modern ATS (AshbyHQ): {score_apply_url(ashby_url, 'Cred')}")
    print("         ✅ Passed!")


def test_url_cleaning_and_unfurling():
    print("\n[test 3] Testing URL Cleaning & Link Unfurling...")
    raw_url = "https://boards.greenhouse.io/swiggy/jobs/12345?utm_source=linkedin&utm_medium=job_alert&ref=123"
    cleaned = clean_tracking_params(raw_url)
    assert "utm_source" not in cleaned, f"Expected clean URL, got {cleaned}"
    assert "boards.greenhouse.io/swiggy/jobs/12345" in cleaned

    # Test unfurler on live URL (Greenhouse handles the redirect cleanly)
    unfurled = unfurl_redirect_url(cleaned)
    assert "greenhouse.io" in unfurled
    print(f"         Cleaned: {cleaned}")
    print(f"         Unfurled: {unfurled}")
    print("         ✅ Passed!")


def test_gemini_lead_extraction():
    print("\n[test 4] Testing Gemini Lead Extraction on Multi-Job Alert & Direct Recruiter...")

    # 1. Multi-Job Portal Digest with embedded links
    portal_email = {
        "from_header": "Wellfound Job Alerts <alerts@wellfound.com>",
        "subject": "5 new Product Manager jobs in India for you",
        "body_text": """
        Hi Jaivik,
        Here are today's top curated product opportunities:

        1. Zepto — Associate Product Manager (Bengaluru, India)
        Drive 10-minute quick-commerce checkout optimization, supply-chain routing algorithms, and customer retention.
        Apply on Wellfound (https://wellfound.com/jobs/99999-zepto-apm)

        2. Razorpay — Product Manager, Merchant Payments (Bengaluru, India)
        Scale payment gateway uptime to 99.99%, launch recurring subscription APIs, and collaborate with enterprise banking partners.
        Apply on Wellfound (https://wellfound.com/jobs/88888-razorpay-pm)

        Sponsored: Get your resume professionally reviewed for $99 at TopResume.
        """,
        "attachments": []
    }

    leads_portal = extract_leads_with_gemini(portal_email)
    print(f"         Extracted {len(leads_portal)} lead(s) from portal digest:")
    for l in leads_portal:
        print(f"           - {l.get('company')} | {l.get('role')} | URL: {l.get('job_url')}")
    assert len(leads_portal) >= 2, f"Expected at least 2 jobs, got {len(leads_portal)}"
    assert any("zepto" in l.get("company", "").lower() for l in leads_portal)
    assert any("razorpay" in l.get("company", "").lower() for l in leads_portal)
    assert not any("topresume" in l.get("company", "").lower() for l in leads_portal), "Promotional ad was not filtered!"

    # 2. Direct Recruiter Email
    recruiter_email = {
        "from_header": "Priya Sharma <priya.sharma@cred.club>",
        "subject": "Product Manager — Founder's Office at CRED",
        "body_text": """
        Hi Jaivik,

        I came across your profile and your consulting background at IQVIA along with your IIM Indore MBA. We are looking for a Product Manager to join our Founder's Office team at CRED in Bengaluru.

        Key responsibilities include defining user engagement loops for high-net-worth members, collaborating with engineering on credit-line algorithms, and launching new financial products from 0 to 1.

        Let me know if you would be open for a quick introductory chat this week.

        Warm regards,
        Priya Sharma
        Head of Product Talent, CRED
        priya.sharma@cred.club
        """,
        "attachments": []
    }

    leads_recruiter = extract_leads_with_gemini(recruiter_email)
    print(f"\n         Extracted {len(leads_recruiter)} lead(s) from direct recruiter email:")
    for l in leads_recruiter:
        print(f"           - {l.get('company')} | {l.get('role')} | Recruiter: {l.get('recruiter_name')} ({l.get('recruiter_email')}) | Direct: {l.get('is_direct_recruiter')}")
    assert len(leads_recruiter) >= 1
    assert leads_recruiter[0].get("is_direct_recruiter") is True
    print("         ✅ Passed!")


def test_state_db_tracking():
    print("\n[test 5] Testing Email Deduplication & State DB Tracking...")
    import time
    init_gmail_tables()
    test_id = f"test_msg_{int(time.time() * 1000)}"
    assert not is_email_parsed(test_id), "Test ID should not be parsed yet"
    mark_email_parsed(test_id, "alerts@wellfound.com", "Wellfound Job Alert", "2026-11-18", 2)
    assert is_email_parsed(test_id), "Test ID should now be marked as parsed"
    print("         ✅ Passed!")


if __name__ == "__main__":
    test_origin_portal_detection()
    test_origin_aware_scoring()
    test_url_cleaning_and_unfurling()
    test_gemini_lead_extraction()
    test_state_db_tracking()
    print("\n🎉 ALL TESTS COMPLETED SUCCESSFULLY!")
