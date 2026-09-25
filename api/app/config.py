import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Main reasoning model (intent parsing / planning).
MODEL_INTENT = os.getenv("SCOUT_MODEL_INTENT", "openai/gpt-oss-120b")
# Bulk per-page extraction — fast model, run once per fetched page.
MODEL_EXTRACT = os.getenv("SCOUT_MODEL_EXTRACT", "openai/gpt-oss-120b")
# Number of DuckDuckGo results to collect per search query during discovery.
SEARCH_RESULTS_PER_QUERY = int(os.getenv("SCOUT_SEARCH_RESULTS", "8"))

DB_PATH = os.getenv("SCOUT_DB_PATH", str(Path(__file__).resolve().parent.parent / "scout.db"))

MAX_URLS_PER_RUN = int(os.getenv("SCOUT_MAX_URLS", "20"))
MAX_PAGE_CHARS = int(os.getenv("SCOUT_MAX_PAGE_CHARS", "9000"))

USER_AGENT = os.getenv(
    "SCOUT_USER_AGENT",
    "ScoutDataBot/0.1 (+https://github.com/Cubix33/data-intelligence-platform; contact: harshdipsaha@gmail.com)",
)
