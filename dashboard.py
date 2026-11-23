"""
dashboard.py — PM Career Copilot: Streamlit Funnel Analytics & Pipeline Tracker
================================================================================
Sprint 4 & 5: Human-in-the-Loop Pipeline Management & Analytics Command Center.

Features:
  - Executive KPI Cards: Total Ingested, Qualified (>=75), Drafts Staged, Applied, Interviews.
  - Quick Action Workflow: Direct Apply links in Column 1, Alumni corporate email column.
  - Multi-Tab Action View:
      * Tab 1: Today's Action Queue (Qualified >= 75 & Staged)
      * Tab 2: Active Pipeline (Applied, Interviewing, Offer)
      * Tab 3: All Discovered Records (Searchable database)
  - One-Click Shortcuts: Deep link to Gmail "Job Copilot" folder and 1-page PDF resume download.
  - Pipeline Analytics & Funnel Charts (Positioned below the action tables).

Run:
  streamlit run dashboard.py
"""

import os
import sqlite3
import pandas as pd
from pathlib import Path
from datetime import datetime
import streamlit as st

# Configure Page
st.set_page_config(
    page_title="PM Career Copilot — Command Center",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded"
)

ROOT = Path(__file__).parent
STATE_DB_PATH = ROOT / "copilot_state.db"
OUTPUT_RESUMES_DIR = ROOT / "output_resumes"


# ─────────────────────────────────────────────────────────────────────────────
# 1. DATABASE ACCESS & CACHE
# ─────────────────────────────────────────────────────────────────────────────

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(STATE_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def clean_source_label(raw_src: str) -> str:
    """Formats raw source labels into clean human-readable badges."""
    if not raw_src or pd.isna(raw_src):
        return "Direct"
    raw = str(raw_src).lower()
    if "gmail:primary:" in raw:
        portal = raw.split("gmail:primary:")[-1]
        return f"Gmail: {portal.capitalize()}"
    elif "gmail:secondary:" in raw:
        portal = raw.split("gmail:secondary:")[-1]
        return f"LinkedIn ({portal.capitalize()})"
    elif "gmail:" in raw:
        portal = raw.split("gmail:")[-1]
        return f"Gmail: {portal.capitalize()}"
    elif "jsearch" in raw:
        return "JSearch India"
    elif "manual:" in raw:
        return "Manual JD"
    elif "dossier" in raw:
        return "Dossier CRM"
    return raw.capitalize()


def load_data() -> pd.DataFrame:
    """Reads all processed jobs from SQLite into a DataFrame."""
    if not STATE_DB_PATH.exists():
        return pd.DataFrame()

    conn = get_connection()
    try:
        df = pd.read_sql_query("""
            SELECT 
                job_hash, source, company, role, location, url, date_posted,
                fit_score, pdf_path, contact_name, contact_title, contact_email,
                alum_tag, draft_status, created_at
            FROM processed_jobs
            ORDER BY created_at DESC
        """, conn)
    except Exception as e:
        st.error(f"Error reading database: {e}")
        df = pd.DataFrame()
    finally:
        conn.close()

    if not df.empty:
        df["fit_score"] = pd.to_numeric(df["fit_score"], errors="coerce").fillna(0).astype(int)
        df["alum_tag"] = df["alum_tag"].fillna("None")
        df["draft_status"] = df["draft_status"].fillna("PENDING")
        df["contact_email"] = df["contact_email"].fillna("—")
        df["contact_name"] = df["contact_name"].fillna("—")
        df["contact_title"] = df["contact_title"].fillna("—")
        df["location"] = df["location"].fillna("India")
        df["url"] = df["url"].fillna("")
        df["source_clean"] = df["source"].apply(clean_source_label)
        df["pdf_file"] = df["pdf_path"].apply(lambda p: Path(p).name if p and Path(p).exists() else "—")

    return df


def update_job_status(job_hash: str, new_status: str):
    """Updates the status of a specific job in SQLite."""
    conn = get_connection()
    with conn:
        conn.execute("""
            UPDATE processed_jobs
            SET draft_status = ?
            WHERE job_hash = ?
        """, (new_status, job_hash))
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# 2. CUSTOM CSS & BRANDING
# ─────────────────────────────────────────────────────────────────────────────

st.markdown("""
<style>
    .metric-card {
        background-color: #f8fafc;
        border-radius: 10px;
        padding: 16px;
        border: 1px solid #e2e8f0;
        box-shadow: 0 1px 3px rgba(0,0,0,0.05);
    }
    .stTabs [data-baseweb="tab-list"] {
        gap: 8px;
    }
    .stTabs [data-baseweb="tab"] {
        padding: 10px 18px;
        font-weight: 600;
        border-radius: 8px 8px 0px 0px;
    }
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# 3. SIDEBAR FILTERS & QUICK LINKS
# ─────────────────────────────────────────────────────────────────────────────

st.sidebar.title("🎯 Career Copilot")
st.sidebar.caption("Candidate: **Jaivik Chauhan**")
st.sidebar.caption("Target: **APM / PM-1 (India)**")
st.sidebar.caption("Primary: `jaivik.chauhan.2026@gmail.com`")
st.sidebar.markdown("---")

st.sidebar.subheader("📬 Quick Gmail Links")
st.sidebar.link_button(
    "📁 Open 'Job Copilot' Staged Drafts",
    "https://mail.google.com/mail/u/0/#label/Job+Copilot",
    use_container_width=True
)
st.sidebar.link_button(
    "📨 Open All Gmail Drafts",
    "https://mail.google.com/mail/u/0/#drafts",
    use_container_width=True
)
st.sidebar.markdown("---")

df = load_data()

if df.empty:
    st.info("No applications recorded yet in `copilot_state.db`. Run `python daily_hunt.py` to start.")
    st.stop()

# Sidebar Filters
st.sidebar.subheader("Filter Applications")

all_statuses = sorted(list(df["draft_status"].dropna().unique()))
selected_statuses = st.sidebar.multiselect("Pipeline Status", all_statuses, default=all_statuses)

all_alums = sorted(list(df["alum_tag"].dropna().unique()))
selected_alums = st.sidebar.multiselect("Alumni Tag", all_alums, default=all_alums)

min_score, max_score = st.sidebar.slider(
    "Fit Score Range",
    min_value=0,
    max_value=100,
    value=(0, 100),
    step=5
)

search_term = st.sidebar.text_input("🔍 Search Company / Role / Email", "").strip().lower()

# Apply Global Filters
filtered_df = df[
    (df["draft_status"].isin(selected_statuses)) &
    (df["alum_tag"].isin(selected_alums)) &
    (df["fit_score"] >= min_score) &
    (df["fit_score"] <= max_score)
]

if search_term:
    filtered_df = filtered_df[
        filtered_df["company"].str.lower().str.contains(search_term, na=False) |
        filtered_df["role"].str.lower().str.contains(search_term, na=False) |
        filtered_df["contact_email"].str.lower().str.contains(search_term, na=False) |
        filtered_df["contact_name"].str.lower().str.contains(search_term, na=False)
    ]


# ─────────────────────────────────────────────────────────────────────────────
# 4. TOP KPI METRICS & ACTION HEADER
# ─────────────────────────────────────────────────────────────────────────────

total_jobs = len(df)
qualified_jobs = len(df[df["fit_score"] >= 75])
qual_rate = (qualified_jobs / total_jobs * 100) if total_jobs > 0 else 0
alum_matches = len(df[df["alum_tag"] != "None"])
drafts_staged = len(df[df["draft_status"].str.contains("STAGED", case=False, na=False)])
applied_jobs = len(df[df["draft_status"].str.upper() == "APPLIED"])
interviews = len(df[df["draft_status"].str.upper().str.contains("INTERVIEW", na=False)])

head_col1, head_col2 = st.columns([3, 1])
with head_col1:
    st.title("📊 PM Career Copilot — Command Center")
    st.markdown("**15-Minute Daily Review:** Check staged roles, click **Apply ↗**, and send pre-drafted pitches.")

with head_col2:
    st.write("")
    st.link_button(
        "🚀 Open Staged Drafts in Gmail",
        "https://mail.google.com/mail/u/0/#label/Job+Copilot",
        type="primary",
        use_container_width=True
    )

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("Total Ingested", f"{total_jobs}", help="Total jobs discovered across JSearch India & Dual Inboxes")
col2.metric("Qualified (75+)", f"{qualified_jobs}", f"{qual_rate:.1f}% Match Rate", help="Postings passing the Rule 5 PM Rubric")
col3.metric("Alum Matches", f"{alum_matches}", f"{'Active' if alum_matches else '0'}", help="Contacts identified as IIM Indore or VIT Vellore alumni")
col4.metric("Drafts Staged", f"{drafts_staged}", help="Personalized pitches staged in jaivik.chauhan.2026@gmail.com")
col5.metric("Applied / Live", f"{applied_jobs + interviews}", f"{interviews} Interviews", help="Applications submitted by the candidate")

st.markdown("---")


# ─────────────────────────────────────────────────────────────────────────────
# 5. INLINE PIPELINE STATUS UPDATER & RESUME INSPECTOR
# ─────────────────────────────────────────────────────────────────────────────

col_act1, col_act2 = st.columns([3, 2])

with col_act1:
    with st.expander("⚡ Quick Status Updater (Advance Pipeline)", expanded=False):
        job_options = {
            f"{r['company']} — {r['role']} (Score: {r['fit_score']} | Status: {r['draft_status']})": r['job_hash']
            for _, r in filtered_df.iterrows()
        }
        if job_options:
            selected_job_label = st.selectbox("Select Role", list(job_options.keys()))
            selected_hash = job_options[selected_job_label]
            status_choices = [
                "LIVE_STAGED", "DRY_RUN_STAGED", "NEEDS_MANUAL_CONTACT",
                "APPLIED", "INTERVIEWING", "OFFER", "REJECTED"
            ]
            col_s1, col_s2 = st.columns([2, 1])
            new_status = col_s1.selectbox("New Stage", status_choices, index=3) # Default APPLIED
            if col_s2.button("Update Status", type="primary", use_container_width=True):
                update_job_status(selected_hash, new_status)
                st.success(f"Updated status to **{new_status}**!")
                st.rerun()
        else:
            st.write("No matching jobs in current filter.")

with col_act2:
    with st.expander("📄 Download Bespoke 1-Page Resume", expanded=False):
        jobs_with_pdf = filtered_df[filtered_df["pdf_path"].str.len() > 0]
        if not jobs_with_pdf.empty:
            pdf_map = {
                f"{r['company']} — {r['role']}": r["pdf_path"]
                for _, r in jobs_with_pdf.iterrows()
            }
            selected_pdf_key = st.selectbox("Select Role PDF", list(pdf_map.keys()))
            pdf_p = Path(pdf_map[selected_pdf_key])
            if pdf_p.exists():
                with open(pdf_p, "rb") as f_pdf:
                    st.download_button(
                        label=f"⬇️ Download {pdf_p.name}",
                        data=f_pdf.read(),
                        file_name=pdf_p.name,
                        mime="application/pdf",
                        use_container_width=True
                    )
            else:
                st.warning("PDF file not found on disk.")
        else:
            st.write("No generated resumes in current view.")


# ─────────────────────────────────────────────────────────────────────────────
# 6. APPLICATIONS TRACKER: TABBED INTERFACE
# ─────────────────────────────────────────────────────────────────────────────

# Helper to render the standard table layout with Apply in Column 1 and Alumni Email
def render_applications_table(data_subset: pd.DataFrame, key_suffix: str):
    if data_subset.empty:
        st.info("No applications match this tab criteria.")
        return

    # Column ordering: URL is Column 1! Contact Email is prominently featured
    cols_to_display = [
        "url", "company", "role", "fit_score", "contact_name",
        "contact_email", "alum_tag", "draft_status", "source_clean",
        "pdf_file", "location", "date_posted"
    ]

    st.dataframe(
        data_subset[cols_to_display],
        column_config={
            "url": st.column_config.LinkColumn(
                "Apply Link",
                help="Click to open the public application page directly",
                display_text="Apply ↗",
                width="small"
            ),
            "company": st.column_config.TextColumn("Company", width="medium"),
            "role": st.column_config.TextColumn("Role Title", width="large"),
            "fit_score": st.column_config.ProgressColumn(
                "Fit Score",
                min_value=0,
                max_value=100,
                format="%d",
                width="small"
            ),
            "contact_name": st.column_config.TextColumn("Contact / Alum", width="medium"),
            "contact_email": st.column_config.TextColumn("Alumni Email", width="medium"),
            "alum_tag": st.column_config.TextColumn("Alum Wedge", width="small"),
            "draft_status": st.column_config.TextColumn("Status", width="medium"),
            "source_clean": st.column_config.TextColumn("Source Channel", width="small"),
            "pdf_file": st.column_config.TextColumn("1-Page Resume", width="medium"),
            "location": st.column_config.TextColumn("Location", width="small"),
            "date_posted": st.column_config.TextColumn("Date", width="small"),
        },
        use_container_width=True,
        hide_index=True,
        key=f"table_{key_suffix}"
    )


# Slice data for the 3 tabs
# Tab 1: Action Queue — Qualified (75+) that are staged or pending review (not yet applied/rejected)
action_mask = (filtered_df["fit_score"] >= 75) & (
    filtered_df["draft_status"].str.contains("STAGED", case=False, na=False) |
    filtered_df["draft_status"].isin(["PENDING", "NEEDS_MANUAL_CONTACT"])
)
action_df = filtered_df[action_mask]

# Tab 2: Active Pipeline — Roles where application is in flight
pipeline_mask = filtered_df["draft_status"].str.upper().isin(["APPLIED", "INTERVIEWING", "OFFER"])
pipeline_df = filtered_df[pipeline_mask]

# Tab 3: Complete database
all_df = filtered_df

tab_action, tab_pipeline, tab_all = st.tabs([
    f"🎯 Today's Action Queue ({len(action_df)})",
    f"📈 Active Pipeline ({len(pipeline_df)})",
    f"🗄️ All Records ({len(all_df)})"
])

with tab_action:
    st.markdown("##### 🚀 Qualified Roles Staged & Ready for Review")
    st.caption("Review the contact and proof point in Gmail, click **Apply ↗** to submit the portal application, then advance stage to **APPLIED**.")
    render_applications_table(action_df, "action")

with tab_pipeline:
    st.markdown("##### 📈 Live Applications & Interview Tracker")
    st.caption("Roles currently in flight. Update stages as recruiters respond or interviews are scheduled.")
    render_applications_table(pipeline_df, "pipeline")

with tab_all:
    st.markdown("##### 🗄️ Complete Job Database")
    st.caption("All ingested roles across JSearch India, Dossier CRM, and Dual Gmail Inboxes (including auto-rejected postings).")
    render_applications_table(all_df, "all")

st.markdown("---")


# ─────────────────────────────────────────────────────────────────────────────
# 7. CHARTS: CONVERSION FUNNEL & SCORE DISTRIBUTION (BELOW TABLES)
# ─────────────────────────────────────────────────────────────────────────────

st.subheader("📈 Pipeline Funnel & Analytics")

c_left, c_right = st.columns([1, 1])

with c_left:
    st.markdown("##### Conversion Funnel")
    funnel_stages = {
        "1. Ingested": total_jobs,
        "2. Qualified (75+)": qualified_jobs,
        "3. PDF Compiled": len(df[df["pdf_path"].str.len() > 0]),
        "4. Draft Staged": drafts_staged,
        "5. Applied": applied_jobs,
        "6. Interview": interviews
    }
    funnel_df = pd.DataFrame(list(funnel_stages.items()), columns=["Stage", "Count"])
    st.bar_chart(funnel_df.set_index("Stage"), color="#2563eb", use_container_width=True)

with c_right:
    st.markdown("##### Fit Score Distribution")
    buckets = {
        "Auto-Reject (0-45)": len(df[df["fit_score"] < 50]),
        "Mid Fit (50-70)": len(df[(df["fit_score"] >= 50) & (df["fit_score"] < 75)]),
        "Qualified (75-85)": len(df[(df["fit_score"] >= 75) & (df["fit_score"] < 90)]),
        "Top Match (90-100)": len(df[df["fit_score"] >= 90])
    }
    bucket_df = pd.DataFrame(list(buckets.items()), columns=["Bucket", "Postings"])
    st.bar_chart(bucket_df.set_index("Bucket"), color="#10b981", use_container_width=True)

st.caption("PM Career Copilot • Built for Jaivik Chauhan • Sprint 5 Dual-Inbox & Origin-Aware Ingestion Active")
