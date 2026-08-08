"""MockFetcher — returns realistic fake job listings without any network calls.

Twelve listings total; four are stable (same source + URL every run) so
deduplication can be demonstrated on a second run.
"""

from __future__ import annotations

from edgedash.agents.base import Agent, AgentResult
from edgedash.config import Config
import edgedash.storage as storage

# ---------------------------------------------------------------------------
# Listing catalogue
# ---------------------------------------------------------------------------

# The first 4 entries have fixed, stable URLs → same id every run → dedup proof.
_STABLE: list[dict] = [
    {
        "title": "Data Analyst",
        "company": "Flipkart",
        "location": "Bengaluru",
        "url": "https://careers.flipkart.com/jobs/da-001",
        "source": "mock",
        "posted_at": "2026-08-01",
        "description": (
            "Work with large-scale e-commerce datasets. "
            "Required: SQL, Python, Tableau, A/B testing, stakeholder reporting."
        ),
    },
    {
        "title": "Junior Data Analyst",
        "company": "Swiggy",
        "location": "Bengaluru",
        "url": "https://careers.swiggy.com/jobs/jda-042",
        "source": "mock",
        "posted_at": "2026-08-02",
        "description": (
            "Support the growth analytics team. "
            "Required: SQL, Excel, Google Sheets, basic Python and pandas."
        ),
    },
    {
        "title": "Senior Data Analyst",
        "company": "Razorpay",
        "location": "Bengaluru",
        "url": "https://razorpay.com/jobs/sda-017",
        "source": "mock",
        "posted_at": "2026-08-01",
        "description": (
            "Own payments funnel analytics end-to-end. "
            "Required: Advanced SQL, Python, dbt, Looker, cross-functional "
            "communication."
        ),
    },
    {
        "title": "Data Analyst – Risk",
        "company": "PhonePe",
        "location": "Bengaluru",
        "url": "https://phonepe.com/careers/da-risk-009",
        "source": "mock",
        "posted_at": "2026-07-31",
        "description": (
            "Analyse fraud and risk signals in transaction data. "
            "Required: SQL, Python, statistical modelling, Spark (good to have)."
        ),
    },
]

# The remaining 8 entries get fresh-looking URLs each catalogue version,
# but within a single run they are only inserted once.
_VARIABLE: list[dict] = [
    {
        "title": "Business Intelligence Analyst",
        "company": "Meesho",
        "location": "Bengaluru",
        "url": "https://meesho.io/jobs/bi-analyst-031",
        "source": "mock",
        "posted_at": "2026-08-03",
        "description": (
            "Build self-serve BI dashboards for category managers. "
            "Required: SQL, Power BI, Python, data modelling, storytelling."
        ),
    },
    {
        "title": "Product Analyst",
        "company": "Zepto",
        "location": "Bengaluru",
        "url": "https://zepto.co/careers/pa-055",
        "source": "mock",
        "posted_at": "2026-08-04",
        "description": (
            "Partner with product managers to define KPIs and run experiments. "
            "Required: SQL, Python, product sense, A/B testing, Mixpanel."
        ),
    },
    {
        "title": "Data Analyst – Supply Chain",
        "company": "Amazon India",
        "location": "Bengaluru",
        "url": "https://amazon.jobs/en/jobs/sc-da-2024",
        "source": "mock",
        "posted_at": "2026-08-02",
        "description": (
            "Optimise inventory and logistics operations using data. "
            "Required: SQL, Python, Excel, Tableau, supply-chain domain knowledge."
        ),
    },
    {
        "title": "Marketing Analyst",
        "company": "Nykaa",
        "location": "Bengaluru",
        "url": "https://careers.nykaa.com/jobs/ma-018",
        "source": "mock",
        "posted_at": "2026-08-05",
        "description": (
            "Measure campaign performance and customer lifetime value. "
            "Required: SQL, Google Analytics, Python or R, cohort analysis."
        ),
    },
    {
        "title": "Data Analyst – Finance",
        "company": "Groww",
        "location": "Bengaluru",
        "url": "https://groww.in/careers/da-fin-007",
        "source": "mock",
        "posted_at": "2026-08-03",
        "description": (
            "Support FP&A with automated reporting and ad-hoc analysis. "
            "Required: SQL, Excel, Python, financial modelling basics."
        ),
    },
    {
        "title": "Senior BI Developer",
        "company": "Infosys BPM",
        "location": "Bengaluru",
        "url": "https://infosys.com/careers/bi-dev-114",
        "source": "mock",
        "posted_at": "2026-07-30",
        "description": (
            "Design and maintain enterprise data warehouse solutions. "
            "Required: SQL Server, SSRS, Power BI, Azure Synapse, Python."
        ),
    },
    {
        "title": "Analyst – Customer Insights",
        "company": "bigbasket",
        "location": "Bengaluru",
        "url": "https://bigbasket.com/careers/ci-analyst-022",
        "source": "mock",
        "posted_at": "2026-08-04",
        "description": (
            "Mine transactional data to understand customer behaviour. "
            "Required: SQL, Python, segmentation, RFM analysis, Tableau."
        ),
    },
    {
        "title": "Lead Data Analyst",
        "company": "CRED",
        "location": "Bengaluru",
        "url": "https://cred.club/careers/lda-003",
        "source": "mock",
        "posted_at": "2026-08-05",
        "description": (
            "Lead a small analytics pod, mentor junior analysts, drive strategy. "
            "Required: SQL, Python, dbt, Looker, 5+ years experience."
        ),
    },
]

_ALL_LISTINGS: list[dict] = _STABLE + _VARIABLE


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class MockFetcher:
    name: str = "mock_fetcher"

    def run(self, config: Config, db_path: str) -> AgentResult:
        try:
            new_count = storage.upsert_listings(db_path, _ALL_LISTINGS)
            return AgentResult(
                agent=self.name,
                status="ok",
                records_touched=new_count,
                notes=(
                    f"Offered {len(_ALL_LISTINGS)} listings to storage; "
                    f"{new_count} were new."
                ),
            )
        except Exception as exc:
            raise RuntimeError(f"MockFetcher failed: {exc}") from exc
