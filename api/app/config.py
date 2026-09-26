import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# ---------------------------------------------------------------------------
# LLM models
# ---------------------------------------------------------------------------

# Main reasoning model (intent parsing / planning).
MODEL_INTENT = os.getenv("SCOUT_MODEL_INTENT", "openai/gpt-oss-120b")
# Bulk per-page extraction — fast model, run once per fetched page.
MODEL_EXTRACT = os.getenv("SCOUT_MODEL_EXTRACT", "openai/gpt-oss-120b")

# ---------------------------------------------------------------------------
# Search / discovery
# ---------------------------------------------------------------------------

# Number of results to collect per search query per engine.
SEARCH_RESULTS_PER_QUERY = int(os.getenv("SCOUT_SEARCH_RESULTS", "8"))

# Brave Search API key (optional; falls back to DuckDuckGo only if not set).
BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")

# SearXNG base URL (optional self-hosted instance).
SEARXNG_URL = os.getenv("SEARXNG_URL", "")

# ---------------------------------------------------------------------------
# Pipeline / capture loop
# ---------------------------------------------------------------------------

DB_PATH = os.getenv("SCOUT_DB_PATH", str(Path(__file__).resolve().parent.parent / "scout.db"))

# Maximum number of capture occasions (query × engine × source_type) per run.
MAX_CAPTURES_PER_RUN = int(os.getenv("SCOUT_MAX_CAPTURES", "12"))

# Maximum URLs to fetch per capture occasion.
MAX_URLS_PER_CAPTURE = int(os.getenv("SCOUT_MAX_URLS_PER_CAPTURE", "6"))

# Legacy: total URLs cap kept for compat with any code that still imports it.
MAX_URLS_PER_RUN = int(os.getenv("SCOUT_MAX_URLS", "20"))

# Stop searching when coverage lower-bound reaches this fraction (overridden by
# DataSpec.target_coverage when the user sets it via the plan editor).
DEFAULT_TARGET_COVERAGE = float(os.getenv("SCOUT_TARGET_COVERAGE", "0.80"))

# Stop searching when marginal yield (new entities per recent capture) drops below this.
MIN_MARGINAL_YIELD = float(os.getenv("SCOUT_MIN_MARGINAL_YIELD", "0.02"))

# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------

# Characters per text chunk passed to the extractor LLM.
CHUNK_SIZE = int(os.getenv("SCOUT_CHUNK_SIZE", "8000"))

# Overlap between adjacent chunks so records at boundaries aren't lost.
CHUNK_OVERLAP = int(os.getenv("SCOUT_CHUNK_OVERLAP", "200"))

# If BeautifulSoup returns fewer than this many chars, assume JS rendering is needed.
MIN_TEXT_CHARS_FOR_JS_FALLBACK = int(os.getenv("SCOUT_MIN_TEXT_CHARS", "200"))

# Maximum extra pages to follow via pagination links.
MAX_PAGINATION_DEPTH = int(os.getenv("SCOUT_MAX_PAGINATION_DEPTH", "3"))

# Max concurrent requests to the same domain.
MAX_CONCURRENT_PER_DOMAIN = int(os.getenv("SCOUT_MAX_CONCURRENT_PER_DOMAIN", "2"))

# Minimum seconds between requests to the same domain.
DOMAIN_REQUEST_DELAY_S = float(os.getenv("SCOUT_DOMAIN_DELAY_S", "1.0"))

# Legacy constant (kept for compat).
MAX_PAGE_CHARS = int(os.getenv("SCOUT_MAX_PAGE_CHARS", "9000"))

USER_AGENT = os.getenv(
    "SCOUT_USER_AGENT",
    "ScoutDataBot/0.1 (+https://github.com/Cubix33/data-intelligence-platform; contact: harshdipsaha@gmail.com)",
)

# ---------------------------------------------------------------------------
# Verifier (Pillar B)
# ---------------------------------------------------------------------------

# HuggingFace model ID for the NLI cross-encoder.
VERIFIER_MODEL = os.getenv(
    "SCOUT_VERIFIER_MODEL", "cross-encoder/nli-deberta-v3-large"
)

# Probability threshold above which a cell is "verified". Cells below this
# threshold are shown as "unverified" and go to the review queue.
# Tune via Learn then Test / conformal risk control on the human-labelled set.
VERIFIER_THRESHOLD = float(os.getenv("SCOUT_VERIFIER_THRESHOLD", "0.5"))

# Set to "false" to disable GPU-based verification (e.g. on machines without CUDA).
VERIFIER_ENABLED = os.getenv("SCOUT_VERIFIER_ENABLED", "true").lower() == "true"
