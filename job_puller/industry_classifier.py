"""Industry classifier — keyword heuristic, no API calls.

Classifies a job into one industry label based on title + description tokens.
Returns None if no industry matches confidently (threshold not met).
"""

from job_puller.scorer import _tokenize

# Minimum keyword hits required to claim an industry label
_CONFIDENCE_THRESHOLD = 2

# Industry keyword dictionary.
# Keys are industry labels shown in the UI.
# Values are lists of keywords/phrases (lowercased, matched as substrings of tokens).
_INDUSTRIES: dict[str, list[str]] = {
    "fintech": [
        "fintech", "banking", "bank", "financial", "payments", "payment",
        "lending", "loan", "credit", "mortgage", "wealth", "investing",
        "brokerage", "trading", "treasury", "kyc", "aml", "bsa",
        "financial institution", "neobank", "interchange", "remittance",
        "money movement", "card", "issuing", "acquiring",
        "financial services", "securities", "asset management", "capital markets",
        "settlement", "clearing", "custody", "investment management",
        "portfolio management", "bnpl", "buy now pay later", "accounts receivable",
        "wire transfer", "ach", "portfolio",
    ],
    "compliance": [
        "compliance", "regulatory", "regulation", "audit", "risk management",
        "governance", "sox", "gdpr", "ccpa", "pci", "hipaa", "aml",
        "anti-money laundering", "sanctions", "regtech", "sar", "bsa",
        "sec", "finra", "cftc", "enforcement", "policy", "legal",
    ],
    "healthcare": [
        "healthcare", "health", "clinical", "patient", "ehr", "emr",
        "hipaa", "medical", "hospital", "provider", "payer", "pharma",
        "pharmaceutical", "biotech", "life sciences", "telehealth",
        "telemedicine", "care", "physician", "lab", "diagnostic",
        "revenue cycle", "prior authorization", "claims",
        "digital health", "health tech", "mental health", "behavioral health",
        "wellness platform", "therapy platform", "health app",
        "remote patient monitoring",
    ],
    "insurtech": [
        "insurance", "insurtech", "underwriting", "claims", "actuary",
        "actuarial", "policy", "premium", "reinsurance", "broker",
        "carrier", "property and casualty", "p&c", "life insurance",
        "benefits", "risk assessment",
    ],
    "developer-tools": [
        "developer", "devops", "ci/cd", "sdk", "api platform",
        "developer experience", "devex", "dx", "open source",
        "platform engineering", "infrastructure", "observability",
        "monitoring", "logging", "deployment", "kubernetes", "cloud native",
        "saas platform", "tooling", "ide",
    ],
    "data-analytics": [
        "data platform", "analytics", "business intelligence", "bi",
        "data warehouse", "snowflake", "databricks", "dbt", "etl", "elt",
        "data engineering", "data science", "machine learning", "ml",
        "artificial intelligence", "ai", "llm", "visualization",
        "reporting", "dashboard", "metrics", "data governance",
    ],
    "cybersecurity": [
        "cybersecurity", "security", "infosec", "identity", "iam",
        "access management", "zero trust", "siem", "soc", "threat",
        "vulnerability", "penetration", "pen test", "endpoint",
        "fraud detection", "authentication", "authorization", "oauth",
        "saml", "encryption", "privacy",
    ],
    "e-commerce": [
        "e-commerce", "ecommerce", "marketplace", "retail", "shopping",
        "checkout", "cart", "catalog", "inventory", "fulfillment",
        "merchant", "seller", "buyer", "storefront", "omnichannel",
        "d2c", "direct to consumer", "subscription",
    ],
    "hr-tech": [
        "hr", "human resources", "hris", "hrms", "payroll", "benefits",
        "workforce", "talent", "recruiting", "applicant tracking", "ats",
        "onboarding", "performance management", "compensation", "employee",
        "people ops", "people operations",
    ],
    "legal-tech": [
        "legal", "legaltech", "contract", "clm", "e-discovery",
        "ediscovery", "litigation", "law firm", "attorney", "counsel",
        "intellectual property", "ip", "trademark", "patent",
        "document management",
    ],
    "proptech": [
        "real estate", "proptech", "property", "mortgage", "lease",
        "tenant", "landlord", "multifamily", "commercial real estate",
        "cre", "listing", "mls", "homebuyer", "home buying",
        "realty", "homeowner", "property management", "renter", "apartment",
        "home search", "rental platform",
    ],
    "edtech": [
        "edtech", "education", "learning", "lms", "curriculum",
        "student", "teacher", "classroom", "course", "training",
        "e-learning", "elearning", "assessment", "tutoring",
        "higher education", "k-12",
    ],
    "logistics": [
        "logistics", "supply chain", "shipping", "freight", "fleet",
        "last mile", "delivery", "fulfillment", "warehouse",
        "transportation", "trucking", "carrier", "routing",
        "inventory management",
    ],
    "crypto-web3": [
        "crypto", "cryptocurrency", "blockchain", "web3", "defi",
        "nft", "token", "wallet", "decentralized", "smart contract",
        "ethereum", "bitcoin", "layer 2", "protocol", "stablecoin",
        "exchange", "dao",
    ],
    "martech": [
        "marketing technology", "martech", "crm", "marketing automation",
        "campaign", "attribution", "customer data platform", "cdp",
        "email marketing", "seo", "sem", "advertising", "ad tech",
        "adtech", "programmatic", "audience", "segmentation",
    ],
    "govtech": [
        "government", "govtech", "public sector", "federal", "state agency",
        "municipal", "civic", "defense", "dod", "agency", "public safety",
        "justice", "court", "dmv", "social services",
    ],
    "gaming": [
        "gaming", "esports", "video game", "game studio", "mobile game",
        "in-game", "game developer", "game engine", "game design",
        "loot box", "battle pass", "player engagement",
    ],
    "travel": [
        "travel", "hotel", "hospitality", "airline", "accommodation",
        "lodging", "vacation rental", "travel management", "itinerary",
        "trip planning", "booking platform", "flight", "reservation system",
    ],
    "saas-b2b": [
        "saas", "b2b", "enterprise software", "platform", "cloud software",
        "subscription software", "software as a service",
    ],
}


def classify(title: str, description: str) -> str | None:
    """Return the best-matching industry label or None if below threshold."""
    combined = f"{title} {description}"
    tokens = _tokenize(combined)
    # Also check raw lowercased text for multi-word phrases
    raw = combined.lower()

    scores: dict[str, int] = {}
    for industry, keywords in _INDUSTRIES.items():
        score = 0
        for kw in keywords:
            if " " in kw:
                # Multi-word phrase: check raw text
                if kw in raw:
                    score += 1
            else:
                if kw in tokens:
                    score += 1
        if score > 0:
            scores[industry] = score

    if not scores:
        return None

    best = max(scores, key=lambda k: scores[k])
    if scores[best] < _CONFIDENCE_THRESHOLD:
        return None

    return best


def classify_all(conn, batch_size: int = 10000) -> int:
    """Classify industry for all jobs missing one. Returns count updated."""
    import sqlite3
    rows = conn.execute(
        """
        SELECT id, title, description FROM jobs
        WHERE industry IS NULL
          AND (status IS NULL OR status != 'dismissed')
        LIMIT ?
        """,
        (batch_size,),
    ).fetchall()

    count = 0
    for row in rows:
        label = classify(row["title"] or "", row["description"] or "")
        conn.execute("UPDATE jobs SET industry = ? WHERE id = ?", (label, row["id"]))
        count += 1

    return count
