"""Entry point — run one full EdgeDash cycle.

Usage
-----
    python run_cycle.py                          # normal run
    python run_cycle.py --dry-run                # print plan, exit, no writes
    python run_cycle.py --force scorer           # force scorer even if nothing unscored
    python run_cycle.py --force fetcher --force scorer   # force multiple agents
    python run_cycle.py --explain                # show full state + decision breakdown
    python run_cycle.py --dry-run --explain      # explain without executing

Environment variables (APIFY_TOKEN, etc.) are loaded here from .env — the
ONE place where dotenv is called, per steering rule 4.  python-dotenv is used
because it handles quoting, export prefixes, and multiline values that a bare
os.environ read would silently mangle; it's a genuine time-saver over rolling
our own parser.
"""

import argparse
import sys

from dotenv import load_dotenv
load_dotenv()  # reads .env from cwd or any parent; no-ops if file is absent

from edgedash.config import load_config
from edgedash.orchestrator import run_cycle


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_cycle.py",
        description="Run one EdgeDash orchestration cycle.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python run_cycle.py\n"
            "  python run_cycle.py --dry-run\n"
            "  python run_cycle.py --dry-run --explain\n"
            "  python run_cycle.py --force scorer\n"
            "  python run_cycle.py --force fetcher --force scorer\n"
            "  python run_cycle.py --explain\n"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help=(
            "Read state, build and print the plan, then exit without executing "
            "anything. No writes, no API calls. Exit code 0."
        ),
    )
    parser.add_argument(
        "--force",
        metavar="AGENT",
        action="append",
        default=[],
        help=(
            "Force the named agent to run even if state says it should be "
            "skipped. Repeatable: --force fetcher --force scorer. "
            "The override is recorded in the cycle summary row."
        ),
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        default=False,
        help=(
            "Print the full SystemState — every value read, with its timestamp "
            "— alongside the decision each value drove. "
            "Use this to debug 'why did it skip that?'"
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    try:
        config = load_config()
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    run_cycle(
        config,
        dry_run=args.dry_run,
        force=args.force,
        explain=args.explain,
    )
