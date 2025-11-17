import argparse
import sys
import logging
from pathlib import Path

from .run_judge import run_judge_on_task, run_judge_on_submission
from .openhands_judge import JudgeLLMConfig

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(asctime)s - %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(
        description="CyberGym RE Judge - Evaluate reverse engineering submissions"
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Command 1: evaluate-task (batch)
    task_parser = subparsers.add_parser(
        "evaluate-task",
        help="Evaluate all unevaluated submissions for a task"
    )
    task_parser.add_argument(
        "--db",
        required=True,
        type=str,
        help="Path to SQLite database (e.g., poc.db)"
    )
    task_parser.add_argument(
        "--data-dir",
        required=True,
        type=str,
        help="Path to task data directory (e.g., cybergym_data/data)"
    )
    task_parser.add_argument(
        "--task",
        required=True,
        type=str,
        help="Task ID to evaluate (e.g., arvo:10400)"
    )
    task_parser.add_argument(
        "--model",
        default="claude-sonnet-4-5-20250929",
        type=str,
        help="Judge LLM model (default: claude-sonnet-4-5-20250929)"
    )
    task_parser.add_argument(
        "--batch-size",
        default=10,
        type=int,
        help="Batch size for database commits (default: 10)"
    )
    task_parser.add_argument(
        "--judge-type",
        choices=["openhands", "llm"],
        default="openhands",
        help="Judge implementation to use (default: openhands)"
    )
    task_parser.add_argument(
        "--timeout",
        default=600,
        type=int,
        help="Timeout per submission in seconds (default: 600)"
    )
    task_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging"
    )

    # Command 2: evaluate-submission (single)
    sub_parser = subparsers.add_parser(
        "evaluate-submission",
        help="Evaluate a single submission (for testing)"
    )
    sub_parser.add_argument(
        "--db",
        required=True,
        type=str,
        help="Path to SQLite database"
    )
    sub_parser.add_argument(
        "--data-dir",
        required=True,
        type=str,
        help="Path to task data directory"
    )
    sub_parser.add_argument(
        "--submission",
        required=True,
        type=str,
        help="Submission ID to evaluate"
    )
    sub_parser.add_argument(
        "--model",
        default="claude-sonnet-4-5-20250929",
        type=str,
        help="Judge LLM model"
    )
    sub_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging"
    )

    args = parser.parse_args()

    # Set logging level
    if hasattr(args, 'verbose') and args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not args.command:
        parser.print_help()
        return 1

    try:
        if args.command == "evaluate-task":
            judge_type = getattr(args, 'judge_type', 'openhands')

            if judge_type == "openhands":
                # Use OpenHands-based judge
                from .openhands_judge import evaluate_task_with_openhands
                result = evaluate_task_with_openhands(
                    db_path=args.db,
                    data_dir=Path(args.data_dir),
                    task_id=args.task,
                    model=args.model,
                    batch_size=args.batch_size,
                    timeout=args.timeout,
                )
            else:
                # Use traditional LLM-based judge
                result = run_judge_on_task(
                    db_path=args.db,
                    data_dir=Path(args.data_dir),
                    task_id=args.task,
                    model=args.model,
                    batch_size=args.batch_size
                )

            print(f"\n✓ Evaluation complete:")
            print(f"  Evaluated: {result['evaluated_count']}")
            print(f"  Failed: {result['failed_count']}")
            print(f"  Total: {result['total']}")
            if result['errors']:
                print(f"\nErrors:")
                for error in result['errors'][:5]:  # Show first 5 errors
                    print(f"  - {error}")
            return 0 if result['failed_count'] == 0 else 1

        elif args.command == "evaluate-submission":
            scores = run_judge_on_submission(
                db_path=args.db,
                data_dir=Path(args.data_dir),
                submission_id=args.submission,
                model=args.model
            )
            print(f"\n✓ Evaluation complete:")
            print(f"  Semantic Similarity: {scores['semantic_similarity']:.3f}")
            print(f"  Correctness Score: {scores['correctness_score']:.3f}")
            print(f"\nReasoning: {scores['judge_reasoning'][:200]}...")
            print(f"\nStrengths: {', '.join(scores['strengths'][:2])}")
            print(f"Weaknesses: {', '.join(scores['weaknesses'][:2])}")
            return 0

        else:
            parser.print_help()
            return 1

    except Exception as e:
        logger.error(f"Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
