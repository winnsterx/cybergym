#!/usr/bin/env python3
"""
Run multiple judge evaluations per pseudocode submission to study judge variance.

Usage:
    python run_judge_variance.py --output-dir judge_variance_study --num-judges 8 --parallel 5 --runtime modal
"""

import argparse
import json
import logging
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from tqdm import tqdm

# Add the examples/agents directory to the path
SCRIPT_DIR = Path(__file__).parent.absolute()
sys.path.insert(0, str(SCRIPT_DIR / "examples/agents/openhands"))

from run import LLMArgs, OpenhandsArgs, TaskArgs as OpenhandsTaskArgs, run_with_configs

from cybergym.eval import get_evaluation_paths, parse_judge_evaluation
from cybergym.eval.client import SubmissionClient, RESubmissionResult
from cybergym.task.types import RUBRICS

# Setup logger
logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

MODAL_SERVER_URL = "https://independentsafetyresearch--cybergym-server-fastapi-app.modal.run"


def run_single_judge(
    submission: dict,
    judge_number: int,
    data_dir: Path,
    eval_paths,
    model: str,
    timeout: int,
    max_iterations: int,
    api_key: str | None,
    base_url: str,
    repo: Path,
    grading_schema: str,
    rubric: str,
    server_url: str | None,
) -> tuple[str, str, int, bool, str | None, dict | None]:
    """
    Run a single judge evaluation.
    Returns (task_id, agent_id, judge_number, success, error_message, scores).
    """
    task_id = submission["task_id"]
    agent_id = submission["agent_id"]
    pseudocode = submission["pseudocode"]
    submission_id = submission["submission_id"]

    try:
        logger.info(f"Starting judge {judge_number} for {task_id} agent {agent_id[:8]}")

        # Get tarball path
        project, task_num = task_id.split(":")
        task_dir = data_dir / project / task_num
        tarball_path = task_dir / "repo-vul.tar.gz"

        if not tarball_path.exists():
            error_msg = f"Tarball not found: {tarball_path}"
            logger.error(f"✗ {error_msg}")
            return (task_id, agent_id, judge_number, False, error_msg, None)

        # Use a dummy run_number based on agent_id hash for directory organization
        run_number = hash(agent_id) % 10000

        judge_dir = eval_paths.judge_dir(task_id, run_number, judge_number)
        judge_dir.mkdir(parents=True, exist_ok=True)

        if api_key:
            os.environ["ANTHROPIC_API_KEY"] = api_key

        llm_args = LLMArgs(
            model=model,
            api_key=api_key,
            base_url=base_url,
            max_output_tokens=4096,
        )

        openhands_args = OpenhandsArgs(
            log_dir=judge_dir,
            tmp_dir=None,
            llm=llm_args,
            max_iter=max_iterations,
            repo=repo,
            silent=True,
            remove_tmp=not eval_paths.keep_tmp,
            timeout=timeout,
        )

        task_args = OpenhandsTaskArgs(
            task_id=task_id,
            data_dir=data_dir,
            server="",
            difficulty="level0",
            evaluation_mode="judge",
            rubric=rubric,
        )

        result_agent_id = run_with_configs(
            openhands_args,
            task_args,
            judge_pseudocode=pseudocode,
            judge_tarball=tarball_path,
            eval_paths=eval_paths,
            run_number=run_number,
            judge_number=judge_number,
        )

        if not result_agent_id:
            error_msg = "Judge validation failed"
            logger.error(f"✗ {error_msg} for {task_id} agent {agent_id[:8]} judge {judge_number}")
            return (task_id, agent_id, judge_number, False, error_msg, None)

        # Parse evaluation.json
        evaluation_file = eval_paths.judge_evaluation_path(task_id, run_number, judge_number)
        if not evaluation_file.exists():
            workspace_eval = eval_paths.judge_workspace_dir(task_id, run_number, judge_number) / "evaluation.json"
            if workspace_eval.exists():
                evaluation_file = workspace_eval

        if not evaluation_file.exists():
            error_msg = f"evaluation.json not found at {evaluation_file}"
            logger.warning(f"✗ {error_msg}")
            return (task_id, agent_id, judge_number, False, error_msg, None)

        with open(evaluation_file) as f:
            scores = json.load(f)

        category_scores_dict, detailed_scores_json = parse_judge_evaluation(scores, grading_schema)

        # Store evaluation via HTTP to Modal server
        if server_url:
            client = SubmissionClient(server_url=server_url)
            client.add_judge_evaluation(
                submission_id=submission_id,
                judge_number=judge_number,
                grading_schema=grading_schema,
                category_scores=category_scores_dict,
                detailed_scores=detailed_scores_json,
            )

        logger.info(f"✓ Judge {judge_number} completed for {task_id} agent {agent_id[:8]}")
        return (task_id, agent_id, judge_number, True, None, category_scores_dict)

    except Exception as e:
        error_msg = str(e)
        logger.error(f"✗ Judge {judge_number} failed for {task_id} agent {agent_id[:8]}: {error_msg}")
        return (task_id, agent_id, judge_number, False, error_msg, None)


def run_judge_wrapper(args: tuple) -> tuple[str, str, int, bool, str | None, dict | None]:
    """Wrapper for multiprocessing."""
    return run_single_judge(*args)


def main():
    parser = argparse.ArgumentParser(
        description="Run multiple judge evaluations per submission to study judge variance",
    )

    parser.add_argument("--output-dir", type=Path, required=True,
                        help="Output directory for judge results")
    parser.add_argument("--num-judges", type=int, default=8,
                        help="Number of judges to run per submission (default: 8)")
    parser.add_argument("--parallel", type=int, default=3,
                        help="Number of parallel judge processes (default: 3)")
    parser.add_argument("--data-dir", type=Path, default=Path("./cybergym_data/data"),
                        help="Directory containing task data")
    parser.add_argument("--model", type=str, default="claude-sonnet-4-5-20250929",
                        help="Model to use for judging")
    parser.add_argument("--rubric", type=str, default="five-point",
                        choices=list(RUBRICS.keys()),
                        help="Rubric to use")
    parser.add_argument("--timeout", type=int, default=1800,
                        help="Timeout in seconds (default: 1800)")
    parser.add_argument("--max-iter", type=int, default=500,
                        help="Maximum iterations (default: 500)")
    parser.add_argument("--api-key", type=str,
                        help="API key (defaults to ANTHROPIC_API_KEY)")
    parser.add_argument("--base-url", type=str, default="",
                        help="Base URL for API")
    parser.add_argument("--repo", type=Path,
                        default=SCRIPT_DIR / "examples/agents/openhands/openhands-repo",
                        help="Path to OpenHands repo")
    parser.add_argument("--server-url", type=str, default=MODAL_SERVER_URL,
                        help="Server URL for querying submissions")
    parser.add_argument("--runtime", type=str, default="modal",
                        choices=["docker", "modal"],
                        help="Runtime: 'docker' (local) or 'modal' (cloud)")
    parser.add_argument("--task-filter", type=str,
                        help="Only process tasks containing this string")
    parser.add_argument("--num", type=int,
                        help="Only process the N most recent submissions")
    parser.add_argument("--keep-tmp", action="store_true",
                        help="Keep temporary files")
    parser.add_argument("--dry-run", action="store_true",
                        help="List submissions without running judges")

    args = parser.parse_args()

    # Validate inputs
    if not args.data_dir.exists():
        logger.error(f"Data directory not found: {args.data_dir}")
        sys.exit(1)

    api_key = args.api_key or os.getenv("ANTHROPIC_API_KEY")
    if not api_key and not args.dry_run:
        logger.warning("No API key found")

    # Set runtime environment variable
    os.environ["OPENHANDS_RUNTIME"] = args.runtime
    logger.info(f"Using runtime: {args.runtime}")

    grading_schema = RUBRICS[args.rubric][1]

    # Query submissions from Modal server
    logger.info(f"Querying submissions from {args.server_url}")
    client = SubmissionClient(server_url=args.server_url)
    submissions = client.list_re_submissions()

    if args.task_filter:
        submissions = [s for s in submissions if args.task_filter in s.task_id]

    # Take most recent N submissions (list is already ordered by submission time)
    if args.num:
        submissions = submissions[-args.num:]

    logger.info(f"Found {len(submissions)} submissions")

    if args.dry_run:
        print("\nSubmissions to process:")
        for s in submissions:
            print(f"  {s.task_id} - {s.agent_id[:8]}...")
        print(f"\nTotal: {len(submissions)} submissions x {args.num_judges} judges = {len(submissions) * args.num_judges} judge runs")
        return

    # Create evaluation paths
    eval_paths = get_evaluation_paths(
        eval_dir=args.output_dir,
        keep_tmp=args.keep_tmp,
    )

    logger.info(f"Output directory: {eval_paths.eval_dir}")

    # Build judge arguments: num_judges per submission
    judge_args_list = []
    for sub in submissions:
        sub_dict = {
            "task_id": sub.task_id,
            "agent_id": sub.agent_id,
            "pseudocode": sub.pseudocode,
            "submission_id": sub.submission_id,
        }
        for judge_num in range(args.num_judges):
            judge_args_list.append((
                sub_dict,
                judge_num,
                args.data_dir,
                eval_paths,
                args.model,
                args.timeout,
                args.max_iter,
                api_key,
                args.base_url,
                args.repo,
                grading_schema,
                args.rubric,
                args.server_url,
            ))

    logger.info(f"Running {len(judge_args_list)} judge evaluations ({len(submissions)} submissions x {args.num_judges} judges)")

    # Run judges in parallel
    results = []
    with ProcessPoolExecutor(max_workers=args.parallel) as executor:
        futures = {executor.submit(run_judge_wrapper, args): args for args in judge_args_list}
        for future in tqdm(as_completed(futures), total=len(futures), desc="Running judges"):
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                logger.error(f"Future failed: {e}")

    # Summary
    successful = sum(1 for r in results if r[3])
    failed = len(results) - successful

    print("\n" + "=" * 80)
    print("JUDGE VARIANCE STUDY SUMMARY")
    print("=" * 80)
    print(f"Submissions: {len(submissions)}")
    print(f"Judges per submission: {args.num_judges}")
    print(f"Total judge runs: {len(results)}")
    print(f"Successful: {successful}")
    print(f"Failed: {failed}")
    if results:
        print(f"Success rate: {successful / len(results) * 100:.1f}%")
    print("=" * 80)

    # Save results summary
    summary_file = eval_paths.eval_dir / "judge_variance_summary.json"
    summary = {
        "submissions": len(submissions),
        "judges_per_submission": args.num_judges,
        "total_runs": len(results),
        "successful": successful,
        "failed": failed,
        "results": [
            {
                "task_id": r[0],
                "agent_id": r[1],
                "judge_number": r[2],
                "success": r[3],
                "error": r[4],
                "scores": r[5],
            }
            for r in results
        ],
    }
    with open(summary_file, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\nResults saved to: {summary_file}")


if __name__ == "__main__":
    main()
