"""Entry point — run one full EdgeDash cycle.

    python run_cycle.py

Environment variables (APIFY_TOKEN, etc.) are loaded here from .env — the
ONE place where dotenv is called, per steering rule 4.  python-dotenv is used
because it handles quoting, export prefixes, and multiline values that a bare
os.environ read would silently mangle; it's a genuine time-saver over rolling
our own parser.
"""

from dotenv import load_dotenv
load_dotenv()  # reads .env from cwd or any parent; no-ops if file is absent

from edgedash.config import load_config
from edgedash.orchestrator import run_cycle

if __name__ == "__main__":
    config = load_config()
    run_cycle(config)
