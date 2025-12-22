#!/usr/bin/env python3
"""
Evaluation runner for CyberGym agents across multiple tasks.

Usage:
    python run_eval.py --task-csv task_lists/tasks.csv --times-per-problem 3 --parallel-requests 5 --output-dir cybergym_eval
"""

import argparse
import atexit
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# Load .env file if it exists
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Add the examples/agents directory to the path to import agent runners
SCRIPT_DIR = Path(__file__).parent.absolute()
sys.path.insert(0, str(SCRIPT_DIR / "examples/agents/openhands"))

from run import LLMArgs, OpenhandsArgs, TaskArgs as OpenhandsTaskArgs, run_with_configs

from cybergym.eval import get_evaluation_paths, parse_judge_evaluation
from cybergym.task.types import RUBRICS
from cybergym.eval.metrics import collect_run_metrics
from cybergym.eval.orchestrator import run_evaluation_pool
from cybergym.eval.reporter import EvalConfig, EvalReporter, print_evaluation_summary

# Setup logger
logger = logging.getLogger(__name__)


def read_tasks_from_csv(csv_path: Path) -> list[str]:
    """Read task IDs from a CSV file."""
    tasks = []
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            task_id = row.get("task") or row.get("task_id")
            if task_id:
                tasks.append(task_id.strip().strip('"'))
    return tasks


def has_ctf_answer(task_id: str, data_dir: Path) -> bool:
    """Check if a CTF task has an answer in its answers.csv."""
    prefix = task_id.split(":")[0]
    answers_file = data_dir / prefix / "answers.csv"
    if not answers_file.exists():
        return False
    with open(answers_file) as f:
        for row in csv.DictReader(f):
            if row.get("task", "").strip().strip('"') == task_id and row.get("flag"):
                return True
    return False


# ============================================================================
# Agent and Judge Runners
# ============================================================================

def run_openhands_agent(
    task_id: str,
    run_number: int,
    eval_paths: Any,
    model: str,
    data_dir: Path,
    server: str,
    timeout: int,
    max_iter: int,
    silent: bool,
    difficulty: str,
    evaluation_mode: str,
    max_output_tokens: int,
    api_key: str | None,
    base_url: str,
    repo: Path,
    rubric: str,
    stripped: bool = False,
    max_poc_attempts: int | None = None,
) -> tuple[str, int, bool, str | None, str | None]:
    """
    Run OpenHands agent for a single task run.
    Returns (task_id, run_number, success, error_message, agent_id).
    """
    try:
        if api_key:
            os.environ["ANTHROPIC_API_KEY"] = api_key

        logger.info(f"Starting task {task_id} run {run_number}")

        agent_dir = eval_paths.agent_dir(task_id, run_number)
        agent_dir.mkdir(parents=True, exist_ok=True)

        llm_args = LLMArgs(
            model=model,
            api_key=api_key,
            base_url=base_url,
            max_output_tokens=max_output_tokens,
        )

        openhands_args = OpenhandsArgs(
            log_dir=agent_dir,
            tmp_dir=None,
            llm=llm_args,
            max_iter=max_iter,
            repo=repo,
            silent=silent,
            remove_tmp=not eval_paths.keep_tmp,
            timeout=timeout,
        )

        task_args = OpenhandsTaskArgs(
            task_id=task_id,
            data_dir=data_dir,
            server=server,
            difficulty=difficulty,
            evaluation_mode=evaluation_mode,
            rubric=rubric,
            stripped=stripped,
            max_poc_attempts=max_poc_attempts,
        )

        agent_id = run_with_configs(
            openhands_args, task_args, eval_paths=eval_paths, run_number=run_number
        )

        if agent_id:
            logger.info(f"✓ Task {task_id} run {run_number} completed (agent_id: {agent_id})")
            return (task_id, run_number, True, None, agent_id)
        else:
            logger.error(f"✗ Task {task_id} run {run_number} failed: validation error")
            return (task_id, run_number, False, "Validation error", None)

    except Exception as e:
        error_msg = str(e)
        logger.error(f"✗ Task {task_id} run {run_number} failed: {error_msg}")
        return (task_id, run_number, False, error_msg, None)


def run_judge_for_submission(
    task_id: str,
    agent_id: str,
    run_number: int,
    judge_number: int,
    data_dir: Path,
    eval_paths: Any,
    model: str,
    timeout: int,
    max_iterations: int,
    api_key: str | None,
    base_url: str,
    repo: Path,
    grading_schema: str,
    rubric: str,
    server_url: str | None = None,
) -> tuple[str, str, int, bool, str | None]:
    """
    Run judge evaluation for a single submission.
    Returns (task_id, agent_id, judge_number, success, error_message).
    """
    try:
        logger.info(f"Starting judge {judge_number} for {task_id} agent {agent_id}")

        from cybergym.eval.client import get_submission_client

        # Get submission using unified client (works for both Docker and Modal)
        client = get_submission_client(
            server_url=server_url,
            db_path=eval_paths.database_path if not server_url else None,
        )

        submission = client.get_re_submission(task_id, agent_id)
        if not submission:
            error_msg = f"No submission found for agent {agent_id}"
            logger.warning(f"✗ {error_msg}")
            return (task_id, agent_id, judge_number, False, error_msg)

        pseudocode = submission.pseudocode
        submission_id = submission.submission_id

        # Get tarball path
        project, task_num = task_id.split(":")
        task_dir = data_dir / project / task_num
        tarball_path = task_dir / "repo-vul.tar.gz"

        if not tarball_path.exists():
            error_msg = f"Tarball not found: {tarball_path}"
            logger.error(f"✗ {error_msg}")
            return (task_id, agent_id, judge_number, False, error_msg)

        judge_dir = eval_paths.judge_dir(task_id, run_number, judge_number)
        judge_dir.mkdir(parents=True, exist_ok=True)

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
            logger.error(f"✗ {error_msg} for {task_id} agent {agent_id} judge {judge_number}")
            return (task_id, agent_id, judge_number, False, error_msg)

        # Parse evaluation.json
        evaluation_file = eval_paths.judge_evaluation_path(task_id, run_number, judge_number)
        if not evaluation_file.exists():
            workspace_eval = eval_paths.judge_workspace_dir(task_id, run_number, judge_number) / "evaluation.json"
            if workspace_eval.exists():
                evaluation_file = workspace_eval

        if not evaluation_file.exists():
            error_msg = f"evaluation.json not found at {evaluation_file}"
            logger.warning(f"✗ {error_msg}")
            return (task_id, agent_id, judge_number, False, error_msg)

        with open(evaluation_file) as f:
            scores = json.load(f)

        category_scores_dict, detailed_scores_json = parse_judge_evaluation(scores, grading_schema)

        # Update database with judge evaluation using unified client
        from cybergym.eval.client import SubmissionClient

        if server_url:
            client = SubmissionClient(server_url=server_url)
        else:
            client = SubmissionClient(db_path=eval_paths.database_path)

        client.add_judge_evaluation(
            submission_id=submission_id,
            judge_number=judge_number,
            grading_schema=grading_schema,
            category_scores=category_scores_dict,
            detailed_scores=detailed_scores_json,
        )

        logger.info(f"✓ Judge {judge_number} completed for {task_id} agent {agent_id}")
        return (task_id, agent_id, judge_number, True, None)

    except Exception as e:
        error_msg = str(e)
        logger.error(f"✗ Judge {judge_number} failed for {task_id} agent {agent_id}: {error_msg}")
        return (task_id, agent_id, judge_number, False, error_msg)


def _agent_wrapper(args: tuple) -> tuple[str, int, bool, str | None, str | None]:
    """Wrapper for multiprocessing."""
    return run_openhands_agent(*args)


def _judge_wrapper(args: tuple) -> tuple[str, str, int, bool, str | None]:
    """Wrapper for multiprocessing."""
    return run_judge_for_submission(*args)


# ============================================================================
# CLI Argument Parsing
# ============================================================================

def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Run CyberGym evaluation across multiple tasks and runs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Required arguments
    parser.add_argument("--task-csv", type=Path, required=True,
                        help="Path to CSV file containing task IDs")
    parser.add_argument("--times-per-problem", type=int, required=True,
                        help="Number of times to run each problem")
    parser.add_argument("--parallel-requests", type=int, required=True,
                        help="Maximum number of parallel requests")
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="Output directory for all tasks and runs")

    # Agent configuration
    parser.add_argument("--agent-type", type=str, default="openhands",
                        choices=["openhands"], help="Agent type to use")
    parser.add_argument("--model", type=str, default="claude-opus-4-5-20251101",
                        help="Model to use")
    parser.add_argument("--max-output-tokens", type=int, default=64000,
                        help="Maximum output tokens")

    # Task configuration
    parser.add_argument("--data-dir", type=Path, default=Path("./cybergym_data/data"),
                        help="Directory containing task data")
    parser.add_argument("--server", type=str, default=None,
                        help="Server address (auto-set based on runtime if not specified)")
    parser.add_argument("--timeout", type=int, default=7200,
                        help="Timeout in seconds (2 hours)")
    parser.add_argument("--max-iter", type=int, default=10000,
                        help="Maximum iterations (set high to avoid iteration limits)")
    parser.add_argument("--silent", action="store_true",
                        help="Suppress agent output")
    parser.add_argument("--difficulty", type=str, default="level0",
                        choices=["level0", "level1", "level2", "level3"],
                        help="Difficulty level")
    parser.add_argument("--evaluation-mode", type=str, default="pseudocode",
                        choices=["exploit", "exploit_library_binary", "exploit_fuzzer_binary", "pseudocode", "ctf"],
                        help="Evaluation mode")
    parser.add_argument("--no-stripped", action="store_false", dest="stripped",
                        help="Use unstripped binaries (with debug symbols) for exploit_library_binary mode")
    parser.set_defaults(stripped=True)

    # API configuration
    parser.add_argument("--api-key", type=str,
                        help="API key (defaults to ANTHROPIC_API_KEY env var)")
    parser.add_argument("--base-url", type=str, default="",
                        help="Base URL for API")
    parser.add_argument("--repo", type=Path,
                        default=SCRIPT_DIR / "examples/agents/openhands/openhands-repo",
                        help="Path to OpenHands repo")
    parser.add_argument("--runtime", type=str, default="modal",
                        choices=["docker", "modal"],
                        help="Runtime: 'docker' (local) or 'modal' (cloud)")

    # Judge configuration
    parser.add_argument("--judge-model", type=str, default="claude-sonnet-4-5-20250929",
                        help="Model to use for judging")
    parser.add_argument("--judge-timeout", type=int, default=1800,
                        help="Timeout for judge evaluation in seconds (30 minutes)")
    parser.add_argument("--judge-max-iter", type=int, default=500,
                        help="Maximum iterations for judge")
    parser.add_argument("--rubric", type=str, default="five-point",
                        choices=list(RUBRICS.keys()),
                        help="Rubric to use for evaluation (five-point, granular)")
    parser.add_argument("--num-of-judges", type=int, default=1,
                        help="Number of judge evaluations per submission")

    # Debug options
    parser.add_argument("--keep-tmp", action="store_true",
                        help="Keep temporary files for debugging")
    parser.add_argument("--server-db-path", type=Path,
                        help="Path to server database")

    # Retry options
    parser.add_argument("--max-run-retries", type=int, default=1,
                        help="Maximum number of attempts per run (default: 1, no retries)")
    parser.add_argument("--retry-delay", type=int, default=60,
                        help="Delay in seconds between run retries (only used if max-run-retries > 1)")

    # POC submission limit (exploit modes only)
    parser.add_argument("--max-poc-attempts", type=int, default=None,
                        help="Max POC submissions per run (exploit/exploit_library_binary/exploit_fuzzer_binary modes)")

    return parser.parse_args()


# ============================================================================
# Main
# ============================================================================

MODAL_SERVER_URL = "https://independentsafetyresearch--cybergym-server-fastapi-app.modal.run"
LOCAL_SERVER_URL = "http://localhost:8666"
# For Docker runtime, localhost refers to the container, not the host
# Use host.docker.internal to reach host services from within containers
DOCKER_SERVER_URL = "http://host.docker.internal:8666"


def main():
    args = parse_args()

    # Set default server based on runtime if not specified
    if args.server is None:
        if args.runtime == "modal":
            args.server = MODAL_SERVER_URL
            logger.info(f"Using Modal server: {args.server}")
        else:
            # For Docker runtime, use host.docker.internal so container can reach host
            args.server = DOCKER_SERVER_URL
            logger.info(f"Using Docker-accessible server: {args.server}")
    elif args.runtime == "docker" and "localhost" in args.server:
        # Translate localhost to host.docker.internal for Docker runtime
        args.server = args.server.replace("localhost", "host.docker.internal")
        logger.info(f"Translated localhost to host.docker.internal: {args.server}")

    # Validate inputs
    if not args.task_csv.exists():
        logger.error(f"Task CSV file not found: {args.task_csv}")
        sys.exit(1)

    if not args.data_dir.exists():
        logger.error(f"Data directory not found: {args.data_dir}")
        sys.exit(1)

    # Get grading schema from rubric mapping
    grading_schema = RUBRICS[args.rubric][1]

    api_key = args.api_key or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("No API key found in arguments or ANTHROPIC_API_KEY env var")

    os.environ["OPENHANDS_RUNTIME"] = args.runtime

    if args.server_db_path and not args.server_db_path.exists():
        logger.error(f"Server database not found: {args.server_db_path}")
        sys.exit(1)

    # Create evaluation paths manager
    eval_paths = get_evaluation_paths(
        eval_dir=args.output_dir,
        keep_tmp=args.keep_tmp,
        server_db_path=args.server_db_path
    )
    atexit.register(eval_paths.cleanup_tmp)

    logger.info(f"Output directory: {eval_paths.eval_dir}")
    logger.info(f"Server database: {eval_paths.database_path}")

    # Read tasks
    tasks = read_tasks_from_csv(args.task_csv)
    if not tasks:
        logger.error("No tasks found in CSV file!")
        sys.exit(1)

    # For CTF mode, skip tasks without answers
    if args.evaluation_mode == "ctf":
        tasks = [t for t in tasks if has_ctf_answer(t, args.data_dir)]
        if not tasks:
            logger.error("No CTF tasks with answers found!")
            sys.exit(1)

    logger.info(f"Found {len(tasks)} tasks")
    logger.info(f"Will run each task {args.times_per_problem} times")
    logger.info(f"Total runs: {len(tasks) * args.times_per_problem}")
    logger.info(f"Parallel requests: {args.parallel_requests}")

    logger.info("=" * 80)
    logger.info("Running agents")
    logger.info("=" * 80)

    eval_start_time = datetime.now().isoformat()
    start_time = time.time()

    # Prepare run arguments
    run_args_list = [
        (
            task_id, run_num, eval_paths, args.model, args.data_dir,
            args.server, args.timeout, args.max_iter, args.silent,
            args.difficulty, args.evaluation_mode, args.max_output_tokens,
            api_key, args.base_url, args.repo, args.rubric, args.stripped,
            args.max_poc_attempts,
        )
        for task_id in tasks
        for run_num in range(args.times_per_problem)
    ]

    # Factory function for judge args
    def make_judge_args(task_id: str, agent_id: str, run_num: int, judge_num: int) -> tuple:
        # Use server URL for Modal runtime to query submissions via HTTP
        judge_server_url = args.server if args.runtime == "modal" else None
        return (
            task_id, agent_id, run_num, judge_num, args.data_dir,
            eval_paths, args.judge_model, args.judge_timeout,
            args.judge_max_iter, api_key, args.base_url, args.repo,
            grading_schema, args.rubric,
            judge_server_url,
        )

    # Run evaluation
    is_re_mode = args.evaluation_mode == "pseudocode"
    agent_results, judge_results = run_evaluation_pool(
        run_args_list=run_args_list,
        agent_runner=_agent_wrapper,
        judge_runner=_judge_wrapper,
        parallel_requests=args.parallel_requests,
        is_re_mode=is_re_mode,
        num_of_judges=args.num_of_judges,
        make_judge_args=make_judge_args,
        max_run_retries=args.max_run_retries,
        retry_delay=args.retry_delay,
    )

    elapsed_time = time.time() - start_time

    # Collect metrics for reporting
    # Use server URL for Modal runtime to query submissions via HTTP
    metrics_server_url = args.server if args.runtime == "modal" else None

    task_run_metrics: dict[str, list[dict]] = {}
    for task_id, run_num, success, error, agent_id in agent_results:
        if task_id not in task_run_metrics:
            task_run_metrics[task_id] = []
        run_metrics = collect_run_metrics(
            task_id=task_id,
            run_number=run_num,
            eval_paths=eval_paths,
            agent_success=success,
            agent_error=error,
            evaluation_mode=args.evaluation_mode,
            grading_schema=grading_schema,
            server_url=metrics_server_url,
            agent_id=agent_id,
        )
        task_run_metrics[task_id].append(run_metrics)

    # Print summary to console
    print_evaluation_summary(
        agent_results=agent_results,
        judge_results=judge_results,
        task_run_metrics=task_run_metrics,
        eval_paths=eval_paths,
        elapsed_time=elapsed_time,
        evaluation_mode=args.evaluation_mode,
    )

    # Generate reports
    config = EvalConfig(
        model=args.model,
        times_per_problem=args.times_per_problem,
        parallel_requests=args.parallel_requests,
        evaluation_mode=args.evaluation_mode,
        difficulty=args.difficulty,
        max_iter=args.max_iter,
        timeout=args.timeout,
        num_of_judges=args.num_of_judges,
        grading_schema=grading_schema,
    )

    reporter = EvalReporter(eval_paths, config, eval_start_time)
    reporter.generate_reports(agent_results, judge_results, task_run_metrics)

    # Exit with error code if any runs failed
    failed = sum(1 for _, _, success, _, _ in agent_results if not success)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s] %(message)s",
    )
    main()
