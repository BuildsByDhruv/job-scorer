"""Manual re-scoring escape hatch (steering rule 18).

Rule 18 says the cycle must NEVER re-score automatically.
This command exists so you can clear scores deliberately when you want to.
The extraction cache is never touched — re-scoring costs zero API calls.

Usage
-----
  python -m edgedash.rescore --all
  python -m edgedash.rescore --id <listing_id>

  --all              Clear every score (prompts for confirmation first).
  --id <listing_id>  Clear one listing's score.
  --db <path>        Override config.db_path.
"""

from __future__ import annotations

import argparse
import sys

_BOLD  = "\033[1m"
_RESET = "\033[0m"
_YELLOW = "\033[33m"
_RED    = "\033[31m"
_DIM    = "\033[2m"


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m edgedash.rescore",
        description=(
            "Clear scores so the next cycle re-scores them.\n"
            "The extraction cache is NEVER cleared — re-scoring costs "
            "zero API calls."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m edgedash.rescore --all\n"
            "  python -m edgedash.rescore --id e9014b66b9a2c3d4"
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--all",
        action="store_true",
        help="Clear scores for every listing.",
    )
    group.add_argument(
        "--id",
        metavar="LISTING_ID",
        help="Clear the score for one listing.",
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        default=None,
        help="Override config.db_path.",
    )
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv()

    from edgedash.config import load_config
    import edgedash.storage as storage

    try:
        config = load_config()
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    db = args.db or config.db_path

    if args.all:
        _run_all(db, storage)
    else:
        _run_one(db, args.id, storage)


def _run_all(db: str, storage: object) -> None:
    # Count how many are currently scored so the message is informative.
    total   = storage.count_total(db)
    unscored_before = storage.count_unscored(db)
    scored  = total - unscored_before

    print(f"\n  {_BOLD}Re-score all listings{_RESET}")
    print(f"  {_DIM}db: {db}{_RESET}")
    print(f"  {scored} scored listings will have their scores cleared.")
    print(f"  {_DIM}(Extraction cache is untouched — no API calls needed.){_RESET}")

    if scored == 0:
        print(f"\n  {_DIM}Nothing to clear — no listings have scores yet.{_RESET}\n")
        return

    print(f"\n  {_YELLOW}This cannot be undone.{_RESET}")
    try:
        answer = input("  Type 'yes' to confirm: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n  Aborted.")
        return

    if answer != "yes":
        print("  Aborted.")
        return

    cleared = storage.clear_score_all(db)
    print(f"\n  {_BOLD}Cleared {cleared} score(s).{_RESET}")
    print(f"  Run  python run_cycle.py  to re-score.\n")


def _run_one(db: str, listing_id: str, storage: object) -> None:
    print(f"\n  {_BOLD}Clearing score for listing {listing_id}{_RESET}")
    print(f"  {_DIM}db: {db}{_RESET}")
    print(f"  {_DIM}(Extraction cache is untouched — no API calls needed.){_RESET}")

    cleared = storage.clear_score_one(db, listing_id)

    if cleared == 0:
        print(f"\n  {_RED}No listing found with id {listing_id!r}.{_RESET}")
        print(f"  Run  python -m edgedash.diagnose  to see valid ids.\n")
        sys.exit(1)

    print(f"\n  {_BOLD}Cleared 1 score.{_RESET}")
    print(f"  Run  python run_cycle.py  to re-score.\n")


if __name__ == "__main__":
    main()
