"""
run_pipeline.py — Canvas-to-HAX ETL Pipeline

Phases
──────
  1  Initialize   — open SQLite DB, ensure schema
  2  Extract      — parse Canvas export into SQLite
  2.5 Evaluate   — LLM-based 8-dimension AI readiness scoring (writes to DB + HTML report)
  3  Transform    — AI-enhance PENDING items with evaluation-informed per-type prompts
  4  Load         — write hax_prep/ markdown/HTML files + JOS manifest

Run `python build_site.py --course_id <id>` afterwards to produce the final HAX site.

Usage
─────
  python run_pipeline.py --course_id 2446743
  python run_pipeline.py --course_id 2446743 --model_provider nebula --workers 5
  python run_pipeline.py --course_id 2446743 --no_ai
  python run_pipeline.py --course_id 2446743 --skip_extract
  python run_pipeline.py --course_id 2446743 --skip_eval --skip_extract
"""

import argparse
import time
import sys
from dotenv import load_dotenv

from sqlite_client import SQLiteClient
from extract import extract_course_data, get_export_dir
from evaluate import evaluate_course_data
from transform import transform_course_data
from load import load_course_site
from ai_client import build_ai_client


def _phase(label: str) -> float:
    """Print a phase header and return the start time."""
    print(f"\n{label}")
    return time.time()


def _done(start: float) -> None:
    """Print elapsed time for the previous phase."""
    print(f"  ⏱  {time.time() - start:.1f}s")


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Canvas-to-HAX ETL Pipeline")
    parser.add_argument("--course_id",      required=True,  type=str)
    parser.add_argument("--db_path",        default="course_pipeline.db")
    parser.add_argument("--export_base",    default="./exports")
    parser.add_argument("--prep_base",      default="./hax_prep")
    parser.add_argument(
        "--model_provider", default="nebula",
        choices=["openai", "anthropic", "gemini", "nebula"],
        help="LLM provider (default: nebula)",
    )
    parser.add_argument("--no_ai",          action="store_true",
                        help="Skip AI; use fallback content only")
    parser.add_argument("--skip_extract",   action="store_true",
                        help="Skip Phase 2 (extract)")
    parser.add_argument("--skip_eval",      action="store_true",
                        help="Skip Phase 2.5 (evaluation)")
    parser.add_argument("--skip_transform", action="store_true",
                        help="Skip Phase 3 (transform)")
    parser.add_argument("--workers",        default=3, type=int,
                        help="Parallel LLM worker threads for eval+transform (default: 3)")
    args = parser.parse_args()

    print("=" * 60)
    print("  Canvas-to-HAX ETL Pipeline")
    print("=" * 60)
    print(f"  Course ID:      {args.course_id}")
    print(f"  Model Provider: {args.model_provider}")
    print(f"  AI Mode:        {'Disabled (--no_ai)' if args.no_ai else 'Enabled'}")
    print(f"  Workers:        {args.workers}")
    print(f"  Database:       {args.db_path}")
    print("=" * 60)

    total_start = time.time()

    try:
        with SQLiteClient(db_path=args.db_path) as db_client:

            # ── Phase 2: Extract ──────────────────────────────────────────────
            if not args.skip_extract:
                t = _phase("📥 Phase 2: Extracting Canvas data...")
                export_dir = get_export_dir(args.course_id, args.export_base)
                extract_course_data(db_client, args.course_id, export_dir)
                _done(t)
            else:
                print("\n⏩ Phase 2: Skipped (--skip_extract)")

            # ── Phase 2.5: Evaluate ───────────────────────────────────────────
            if not args.skip_eval and not args.no_ai:
                t = _phase("🔬 Phase 2.5: Evaluating AI readiness...")
                ai = build_ai_client(args.model_provider)
                evaluate_course_data(
                    db_client, args.course_id, ai,
                    workers=args.workers,
                )
                _done(t)
            else:
                reason = "--no_ai" if args.no_ai else "--skip_eval"
                print(f"\n⏩ Phase 2.5: Skipped ({reason})")

            # ── Phase 3: Transform ────────────────────────────────────────────
            if not args.skip_transform:
                t = _phase("🤖 Phase 3: Enhancing content...")
                transform_course_data(
                    db_client, args.course_id,
                    model_provider=args.model_provider,
                    no_ai=args.no_ai,
                    workers=args.workers,
                )
                _done(t)
            else:
                print("\n⏩ Phase 3: Skipped (--skip_transform)")

            # ── Phase 4: Load ─────────────────────────────────────────────────
            t = _phase("📤 Phase 4: Writing hax_prep output...")
            load_course_site(
                db_client, args.course_id,
                export_base=args.export_base,
                prep_base=args.prep_base,
            )
            _done(t)

    except FileNotFoundError as exc:
        print(f"\n❌ Error: {exc}", file=sys.stderr)
        print("   Check that the course export exists under:", args.export_base, file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"\n❌ Unexpected error: {exc}", file=sys.stderr)
        raise

    total = time.time() - total_start
    print("\n" + "=" * 60)
    print(f"  Pipeline Complete!  ({total:.1f}s total)")
    print("=" * 60)
    print(f"  Next: python build_site.py --course_id {args.course_id}")


if __name__ == "__main__":
    main()
