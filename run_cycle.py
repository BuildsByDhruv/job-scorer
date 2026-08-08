"""Entry point — run one full EdgeDash cycle.

    python run_cycle.py
"""

from edgedash.config import load_config
from edgedash.orchestrator import run_cycle

if __name__ == "__main__":
    config = load_config()
    run_cycle(config)
