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
import multiprocessing as mp
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from tqdm import tqdm

# Load .env file if it exists
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # python-dotenv not installed, skip loading .env file
    pass

# Add the examples/agents directory to the path to import agent runners
SCRIPT_DIR = Path(__file__).parent.absolute()
sys.path.insert(0, str(SCRIPT_DIR / "examples/agents/openhands"))

from run import LLMArgs, OpenhandsArgs, TaskArgs as OpenhandsTaskArgs, run_with_configs

from cybergym.eval import get_evaluation_paths
from cybergym.task.types import TaskDifficulty

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


def run_openhands_agent(
    task_id: str,
    run_number: int,
    eval_paths: Any,  # EvaluationPaths
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
) -> tuple[str, int, bool, str | None, str | None]:
    """
    Run OpenHands agent for a single task run.
    Returns (task_id, run_number, success, error_message, agent_id).
    agent_id is only populated on success for RE mode.
    """
    try:
        # Set up API key in environment for this process
        if api_key:
            os.environ["ANTHROPIC_API_KEY"] = api_key

        logger.info(f"Starting task {task_id} run {run_number}")

        # Set up directories using centralized path management
        # Note: agent_id not known yet, will be created in run_with_configs
        # We pass the agent directory, and run.py will create the full structure
        agent_dir = eval_paths.agent_dir(task_id, run_number)
        agent_dir.mkdir(parents=True, exist_ok=True)

        # Create LLM args
        llm_args = LLMArgs(
            model=model,
            api_key=api_key,
            base_url=base_url,
            max_output_tokens=max_output_tokens,
        )

        # Create OpenHands args with new path structure
        openhands_args = OpenhandsArgs(
            log_dir=agent_dir,  # Will contain logs/, workspace/, trajectory/, etc.
            tmp_dir=None,  # Will be set by run.py using eval_paths
            llm=llm_args,
            max_iter=max_iter,
            repo=repo,
            silent=silent,
            remove_tmp=not eval_paths.keep_tmp,
            timeout=timeout,
        )

        # Create task args
        task_args = OpenhandsTaskArgs(
            task_id=task_id,
            data_dir=data_dir,
            server=server,
            difficulty=difficulty,
            evaluation_mode=evaluation_mode,
        )

        # Run the task, passing eval_paths for path management
        agent_id = run_with_configs(openhands_args, task_args, eval_paths=eval_paths, run_number=run_number)

        if agent_id:
            logger.info(
                f"✓ Task {task_id} run {run_number} completed successfully (agent_id: {agent_id})"
            )
            return (task_id, run_number, True, None, agent_id)
        else:
            logger.error(f"✗ Task {task_id} run {run_number} failed: validation error")
            return (task_id, run_number, False, "Validation error", None)

    except Exception as e:
        error_msg = str(e)
        logger.error(f"✗ Task {task_id} run {run_number} failed with exception: {error_msg}")
        return (task_id, run_number, False, error_msg, None)


def run_agent_wrapper(args: tuple) -> tuple[str, int, bool, str | None, str | None]:
    """Wrapper function for multiprocessing that unpacks arguments."""
    return run_openhands_agent(*args)


def run_judge_for_submission(
    task_id: str,
    agent_id: str,
    run_number: int,
    data_dir: Path,
    eval_paths: Any,  # EvaluationPaths
    model: str,
    timeout: int,
    max_iterations: int,
    api_key: str | None,
    base_url: str,
    repo: Path,
) -> tuple[str, str, bool, str | None]:
    """
    Run judge evaluation for a single submission using run.py in judge mode.
    Returns (task_id, agent_id, success, error_message).
    """
    try:
        logger.info(f"Starting judge evaluation for {task_id} agent {agent_id}")

        # Get submission from database
        from sqlalchemy.orm import Session
        from cybergym.server.pocdb import RESubmission, init_engine

        db_path = eval_paths.database_path
        engine = init_engine(db_path)
        with Session(engine) as session:
            submission = (
                session.query(RESubmission)
                .filter(
                    RESubmission.task_id == task_id,
                    RESubmission.agent_id == agent_id,
                    RESubmission.evaluated_at == None
                )
                .first()
            )

            if not submission:
                error_msg = f"No unevaluated submission found for agent {agent_id}"
                logger.warning(f"✗ {error_msg}")
                return (task_id, agent_id, False, error_msg)

            pseudocode = submission.pseudocode
            submission_id = submission.submission_id

        # Get tarball path
        project, task_num = task_id.split(":")
        task_dir = data_dir / project / task_num
        tarball_path = task_dir / "repo-vul.tar.gz"

        if not tarball_path.exists():
            error_msg = f"Tarball not found: {tarball_path}"
            logger.error(f"✗ {error_msg}")
            return (task_id, agent_id, False, error_msg)

        # Set up directories using centralized path management
        judge_dir = eval_paths.judge_dir(task_id, run_number)
        judge_dir.mkdir(parents=True, exist_ok=True)

        # Create LLM args
        llm_args = LLMArgs(
            model=model,
            api_key=api_key,
            base_url=base_url,
            max_output_tokens=4096,
        )

        # Create OpenHands args with new path structure
        openhands_args = OpenhandsArgs(
            log_dir=judge_dir,  # Will contain logs/, workspace/, trajectory/, etc.
            tmp_dir=None,  # Will be set by run.py using eval_paths
            llm=llm_args,
            max_iter=max_iterations,
            repo=repo,
            silent=True,
            remove_tmp=not eval_paths.keep_tmp,
            timeout=timeout,
        )

        # Create task args for judge mode
        task_args = OpenhandsTaskArgs(
            task_id=task_id,
            data_dir=data_dir,
            server="",  # Not needed for judge
            difficulty="level0",  # Not needed for judge
            evaluation_mode="judge",
        )

        # Run judge using run.py
        result_agent_id = run_with_configs(
            openhands_args,
            task_args,
            judge_pseudocode=pseudocode,
            judge_tarball=tarball_path,
            eval_paths=eval_paths,
            run_number=run_number,
        )

        if not result_agent_id:
            error_msg = "Judge validation failed"
            logger.error(f"✗ {error_msg} for {task_id} agent {agent_id}")
            return (task_id, agent_id, False, error_msg)

        # Parse evaluation.json from judge workspace
        evaluation_file = eval_paths.judge_evaluation_path(task_id, run_number)

        # Also check in workspace subdirectory for backward compatibility
        if not evaluation_file.exists():
            workspace_eval = eval_paths.judge_workspace_dir(task_id, run_number) / "evaluation.json"
            if workspace_eval.exists():
                evaluation_file = workspace_eval

        if not evaluation_file.exists():
            error_msg = f"evaluation.json not found at {evaluation_file}"
            logger.warning(f"✗ {error_msg}")
            return (task_id, agent_id, False, error_msg)

        # Read and update database
        with open(evaluation_file) as f:
            scores = json.load(f)

        from cybergym.server.pocdb import now
        with Session(engine) as session:
            db_submission = session.query(RESubmission).filter_by(
                submission_id=submission_id
            ).first()

            if db_submission:
                db_submission.semantic_similarity = scores.get("semantic_similarity", 0.0)
                db_submission.correctness_score = scores.get("correctness_score", 0.0)
                db_submission.judge_reasoning = scores.get("judge_reasoning", "")
                db_submission.strengths = json.dumps(scores.get("strengths", []))
                db_submission.weaknesses = json.dumps(scores.get("weaknesses", []))
                db_submission.evaluated_at = now()
                session.commit()

        logger.info(f"✓ Judge completed for {task_id} agent {agent_id}")
        return (task_id, agent_id, True, None)

    except Exception as e:
        error_msg = str(e)
        logger.error(f"✗ Judge failed for {task_id} agent {agent_id}: {error_msg}")
        return (task_id, agent_id, False, error_msg)


def run_judge_wrapper(args: tuple) -> tuple[str, str, bool, str | None]:
    """Wrapper function for multiprocessing that unpacks judge arguments."""
    return run_judge_for_submission(*args)


def calculate_statistics(values: list[float]) -> dict[str, float]:
    """
    Calculate statistics (median, min, max, mean) for a list of values.

    Args:
        values: List of numeric values

    Returns:
        Dictionary with median, min, max, mean, and count
    """
    if not values:
        return {
            "median": None,
            "min": None,
            "max": None,
            "mean": None,
            "count": 0,
        }

    sorted_values = sorted(values)
    n = len(sorted_values)

    # Calculate median
    if n % 2 == 0:
        median = (sorted_values[n // 2 - 1] + sorted_values[n // 2]) / 2
    else:
        median = sorted_values[n // 2]

    return {
        "median": round(median, 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
        "mean": round(sum(values) / len(values), 3),
        "count": n,
    }


def collect_run_metrics(
    task_id: str,
    run_number: int,
    eval_paths: Any,
    agent_success: bool,
    agent_error: str | None,
    evaluation_mode: str = "reverse_engineering",
) -> dict:
    """
    Collect metrics for a single run from the evaluation.json file or database.

    Args:
        task_id: Task identifier
        run_number: Run number
        eval_paths: EvaluationPaths instance
        agent_success: Whether the agent run succeeded
        agent_error: Error message if agent failed
        evaluation_mode: Evaluation mode (reverse_engineering, flare-on, exploit)

    Returns:
        Dictionary with run_id, status, and metrics
    """
    # Base result for all modes
    result = {
        "run_id": run_number,
        "status": "success" if agent_success else "failed",
    }

    # For flare-on mode, only track correct/incorrect
    if evaluation_mode == "flare-on":
        if not agent_success:
            result["error"] = agent_error
            result["correct"] = False
            return result
        try:
            from sqlalchemy.orm import Session
            from cybergym.server.pocdb import init_engine, query_flareon_submissions

            db_path = eval_paths.database_path
            engine = init_engine(db_path)
            with Session(engine) as session:
                submissions = query_flareon_submissions(
                    session,
                    task_id=task_id,
                    correct=1,  # Only get correct submissions
                )

                if submissions:
                    # Agent succeeded if there's at least one correct submission
                    result["correct"] = True
                else:
                    # Check if there are any submissions at all
                    all_submissions = query_flareon_submissions(session, task_id=task_id)
                    if all_submissions:
                        result["correct"] = False
                    else:
                        result["error"] = "No submissions found"
                        result["correct"] = False

        except Exception as e:
            logger.warning(f"Failed to query flare-on submissions for {task_id} run {run_number}: {e}")
            result["error"] = f"Failed to query submissions: {str(e)}"
            result["correct"] = False

        return result

    # For RE mode, add semantic_similarity and correctness_score fields and try to load evaluation.json
    result["semantic_similarity"] = None
    result["correctness_score"] = None

    if not agent_success:
        result["error"] = agent_error
        return result
    evaluation_file = eval_paths.judge_evaluation_path(task_id, run_number)

    # Also check in workspace subdirectory for backward compatibility
    if not evaluation_file.exists():
        workspace_eval = eval_paths.judge_workspace_dir(task_id, run_number) / "evaluation.json"
        if workspace_eval.exists():
            evaluation_file = workspace_eval

    if evaluation_file.exists():
        try:
            with open(evaluation_file) as f:
                scores = json.load(f)

            result["semantic_similarity"] = scores.get("semantic_similarity")
            result["correctness_score"] = scores.get("correctness_score")
            result["judge_reasoning"] = scores.get("judge_reasoning", "")

        except Exception as e:
            logger.warning(f"Failed to read evaluation file for {task_id} run {run_number}: {e}")
            result["error"] = f"Failed to read evaluation: {str(e)}"
    else:
        result["error"] = "Evaluation file not found"

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Run CyberGym evaluation across multiple tasks and runs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Required arguments
    parser.add_argument(
        "--task-csv",
        type=Path,
        required=True,
        help="Path to CSV file containing task IDs",
    )
    parser.add_argument(
        "--times-per-problem",
        type=int,
        required=True,
        help="Number of times to run each problem",
    )
    parser.add_argument(
        "--parallel-requests",
        type=int,
        required=True,
        help="Maximum number of parallel requests / number of parallel agents",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Output directory for all tasks and runs",
    )

    # Agent configuration
    parser.add_argument(
        "--agent-type",
        type=str,
        default="openhands",
        choices=["openhands"],  # Will extend later
        help="Agent type to use (default: openhands)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="claude-sonnet-4-5-20250929",
        help="Model to use (default: claude-sonnet-4-5-20250929)",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=64000,
        help="Maximum output tokens (default: 64000)",
    )

    # Task configuration
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("./cybergym_data/data"),
        help="Directory containing task data (default: ./cybergym_data/data)",
    )
    parser.add_argument(
        "--server",
        type=str,
        default="http://10.138.0.2:8666",
        help="Server address (default: http://10.138.0.2:8666)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1200,
        help="Timeout in seconds (default: 1200)",
    )
    parser.add_argument(
        "--max-iter",
        type=int,
        default=100,
        help="Maximum iterations (default: 100)",
    )
    parser.add_argument(
        "--silent",
        action="store_true",
        help="Suppress agent output",
    )
    parser.add_argument(
        "--difficulty",
        type=str,
        default="level0",
        choices=["level0", "level1", "level2", "level3"],
        help="Difficulty level (default: level0)",
    )
    parser.add_argument(
        "--evaluation-mode",
        type=str,
        default="reverse_engineering",
        choices=["exploit", "reverse_engineering", "flare-on"],
        help="Evaluation mode (default: reverse_engineering)",
    )

    # API configuration
    parser.add_argument(
        "--api-key",
        type=str,
        help="API key (defaults to ANTHROPIC_API_KEY environment variable)",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default="",
        help="Base URL for API (default: empty)",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=SCRIPT_DIR / "examples/agents/openhands/openhands-repo",
        help="Path to OpenHands repo (default: ./examples/agents/openhands/openhands-repo)",
    )

    # Judge configuration
    parser.add_argument(
        "--judge-model",
        type=str,
        default="claude-sonnet-4-5-20250929",
        help="Model to use for judging (default: claude-sonnet-4-5-20250929)",
    )
    parser.add_argument(
        "--judge-timeout",
        type=int,
        default=1200,
        help="Timeout for judge evaluation in seconds (default: 1200)",
    )
    parser.add_argument(
        "--judge-max-iter",
        type=int,
        default=100,
        help="Maximum iterations for judge (default: 100)",
    )

    # Debug/development options
    parser.add_argument(
        "--keep-tmp",
        action="store_true",
        help="Keep temporary files for debugging (default: remove after completion)",
    )
    parser.add_argument(
        "--server-db-path",
        type=Path,
        help="Path to server database (default: ./server_poc/poc.db)",
    )

    args = parser.parse_args()

    # Validate inputs
    if not args.task_csv.exists():
        logger.error(f"Task CSV file not found: {args.task_csv}")
        sys.exit(1)

    if not args.data_dir.exists():
        logger.error(f"Data directory not found: {args.data_dir}")
        sys.exit(1)

    # Get API key from environment if not provided
    api_key = args.api_key or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("No API key found in arguments or ANTHROPIC_API_KEY environment variable")

    # Validate server database path if provided
    if args.server_db_path and not args.server_db_path.exists():
        logger.error(f"Server database not found: {args.server_db_path}")
        sys.exit(1)

    # Create evaluation paths manager
    eval_paths = get_evaluation_paths(
        eval_dir=args.output_dir,
        keep_tmp=args.keep_tmp,
        server_db_path=args.server_db_path
    )

    # Register cleanup for tmp directory on exit
    atexit.register(eval_paths.cleanup_tmp)

    logger.info(f"Output directory: {eval_paths.eval_dir}")
    logger.info(f"Server database: {eval_paths.database_path}")
    if args.keep_tmp:
        logger.info(f"Temporary files will be kept in: {eval_paths.tmp_base}")
    else:
        logger.info(f"Temporary files will be auto-cleaned from: {eval_paths.tmp_base}")

    # Read tasks from CSV
    tasks = read_tasks_from_csv(args.task_csv)
    if not tasks:
        logger.error("No tasks found in CSV file!")
        sys.exit(1)

    logger.info(f"Found {len(tasks)} tasks in CSV")
    logger.info(f"Will run each task {args.times_per_problem} times")
    logger.info(f"Total runs: {len(tasks) * args.times_per_problem}")
    logger.info(f"Parallel requests: {args.parallel_requests}")

    # Prepare all task×run combinations
    logger.info("=" * 80)
    logger.info("Running agents")
    logger.info("=" * 80)

    # Store evaluation start time
    eval_start_time = datetime.now().isoformat()

    run_args_list = []
    for task_id in tasks:
        for run_num in range(args.times_per_problem):
            run_args = (
                task_id,
                run_num,
                eval_paths,  # Pass eval_paths instead of task_dir
                args.model,
                args.data_dir,
                args.server,
                args.timeout,
                args.max_iter,
                args.silent,
                args.difficulty,
                args.evaluation_mode,
                args.max_output_tokens,
                api_key,
                args.base_url,
                args.repo,
            )
            run_args_list.append(run_args)

    # Execute agents and judges with unified worker pool
    agent_results = []
    judge_results = []
    start_time = time.time()

    # For RE mode, we need to track judge tasks
    is_re_mode = args.evaluation_mode == "reverse_engineering"

    if args.parallel_requests > 1:
        logger.info(f"Using multiprocessing with {args.parallel_requests} workers")

        with mp.Pool(args.parallel_requests) as pool:
            # Submit all agent tasks
            agent_futures = {}
            for run_args in run_args_list:
                future = pool.apply_async(run_agent_wrapper, (run_args,))
                agent_futures[future] = run_args

            # Track progress and queue judges as agents complete
            judge_futures = {}
            completed_agents = 0
            total_agents = len(run_args_list)

            with tqdm(total=total_agents, desc="Running agents") as pbar:
                # Poll for completed agents
                while completed_agents < total_agents:
                    for future in list(agent_futures.keys()):
                        if future.ready():
                            try:
                                result = future.get()
                                task_id, run_num, success, error, agent_id = result
                                agent_results.append(result)
                                completed_agents += 1
                                pbar.update(1)

                                # Queue judge if RE mode and agent succeeded
                                if is_re_mode and success and agent_id:
                                    judge_args = (
                                        task_id,
                                        agent_id,
                                        run_num,
                                        args.data_dir,
                                        eval_paths,
                                        args.judge_model,
                                        args.judge_timeout,
                                        args.judge_max_iter,
                                        api_key,
                                        args.base_url,
                                        args.repo,
                                    )
                                    judge_future = pool.apply_async(run_judge_wrapper, (judge_args,))
                                    judge_futures[judge_future] = (task_id, agent_id)
                                    logger.info(f"Queued judge for {task_id} agent {agent_id}")

                            except Exception as e:
                                logger.error(f"Error getting agent result: {e}")
                                completed_agents += 1
                                pbar.update(1)

                            del agent_futures[future]

                    time.sleep(0.1)  # Small delay to avoid busy-waiting

            # Wait for all judges to complete
            if judge_futures:
                logger.info(f"\nWaiting for {len(judge_futures)} judge evaluations to complete...")
                with tqdm(total=len(judge_futures), desc="Running judges") as pbar:
                    completed_judges = 0
                    total_judges = len(judge_futures)

                    while completed_judges < total_judges:
                        for future in list(judge_futures.keys()):
                            if future.ready():
                                try:
                                    result = future.get()
                                    judge_results.append(result)
                                    completed_judges += 1
                                    pbar.update(1)
                                except Exception as e:
                                    logger.error(f"Error getting judge result: {e}")
                                    completed_judges += 1
                                    pbar.update(1)

                                del judge_futures[future]

                        time.sleep(0.1)
    else:
        logger.info("Running agents sequentially")
        for run_args in tqdm(run_args_list, desc="Running agents"):
            result = run_openhands_agent(*run_args)
            task_id, run_num, success, error, agent_id = result
            agent_results.append(result)

            # Run judge immediately after agent in sequential mode
            if is_re_mode and success and agent_id:
                judge_args = (
                    task_id,
                    agent_id,
                    run_num,
                    args.data_dir,
                    eval_paths,
                    args.judge_model,
                    args.judge_timeout,
                    args.judge_max_iter,
                    api_key,
                    args.base_url,
                    args.repo,
                )
                judge_result = run_judge_for_submission(*judge_args)
                judge_results.append(judge_result)

    # Print summary
    elapsed_time = time.time() - start_time
    successful = sum(1 for _, _, success, _, _ in agent_results if success)
    failed = len(agent_results) - successful

    print("\n" + "=" * 80)
    print("EVALUATION SUMMARY")
    print("=" * 80)
    print(f"Total agent runs: {len(agent_results)}")
    print(f"Completed: {successful}")
    print(f"Failed: {failed}")
    print(f"Completion rate: {successful / len(agent_results) * 100:.1f}%")
    print(f"Time elapsed: {elapsed_time:.2f}s")
    print(f"Output directory: {eval_paths.eval_dir}")
    print(f"Database: {eval_paths.database_path}")

    # Print judge summary if RE mode
    if is_re_mode and judge_results:
        judge_successful = sum(1 for _, _, success, _ in judge_results if success)
        judge_failed = len(judge_results) - judge_successful
        print(f"\nJudge Evaluations:")
        print(f"  Total: {len(judge_results)}")
        print(f"  Successful: {judge_successful}")
        print(f"  Failed: {judge_failed}")
        if judge_results:
            print(f"  Success rate: {judge_successful / len(judge_results) * 100:.1f}%")

    print("=" * 80)

    # Build task results summary
    task_results = {}
    for task_id, run_num, success, error, agent_id in agent_results:
        if task_id not in task_results:
            task_results[task_id] = {"total": 0, "success": 0, "failed": 0}
        task_results[task_id]["total"] += 1
        if success:
            task_results[task_id]["success"] += 1
        else:
            task_results[task_id]["failed"] += 1

    # Collect per-run metrics for each task (needed for Flare-On solve rate)
    task_run_metrics = {}  # task_id -> list of run metrics
    for task_id, run_num, success, error, agent_id in agent_results:
        if task_id not in task_run_metrics:
            task_run_metrics[task_id] = []

        # Collect metrics for this run
        run_metrics = collect_run_metrics(
            task_id=task_id,
            run_number=run_num,
            eval_paths=eval_paths,
            agent_success=success,
            agent_error=error,
            evaluation_mode=args.evaluation_mode,
        )
        task_run_metrics[task_id].append(run_metrics)

    print("\nPer-task results:")
    is_flareon_mode = args.evaluation_mode == "flare-on"
    for task_id in sorted(task_results.keys()):
        stats = task_results[task_id]
        if is_flareon_mode:
            # Calculate solve rate from run metrics
            run_results = task_run_metrics.get(task_id, [])
            solved = sum(1 for r in run_results if r.get("correct") == True)
            completion_rate = stats["success"] / stats["total"] * 100
            solve_rate = solved / stats["total"] * 100
            print(
                f"  {task_id}: {stats['success']}/{stats['total']} completed ({completion_rate:.1f}%), "
                f"{solved}/{stats['total']} solved ({solve_rate:.1f}%)"
            )
        else:
            success_rate = stats["success"] / stats["total"] * 100
            print(
                f"  {task_id}: {stats['success']}/{stats['total']} successful ({success_rate:.1f}%)"
            )

    # Print failed agent runs if any
    if failed > 0:
        print("\nFailed agent runs:")
        for task_id, run_num, success, error, agent_id in agent_results:
            if not success:
                print(f"  ✗ {task_id} run {run_num}: {error}")

    # Print failed judge runs if any
    if is_re_mode and judge_results:
        judge_failed_list = [(task_id, agent_id, error) for task_id, agent_id, success, error in judge_results if not success]
        if judge_failed_list:
            print("\nFailed judge evaluations:")
            for task_id, agent_id, error in judge_failed_list:
                print(f"  ✗ {task_id} agent {agent_id}: {error}")

    # Print overall solve rate for Flare-On mode
    is_flareon_mode = args.evaluation_mode == "flare-on"
    if is_flareon_mode:
        total_solved = sum(
            sum(1 for r in task_run_metrics.get(tid, []) if r.get("correct") == True)
            for tid in task_results.keys()
        )
        solve_rate = total_solved / len(agent_results) * 100 if agent_results else 0
        print(f"\nOverall Solve Rate: {total_solved}/{len(agent_results)} ({solve_rate:.1f}%)")

    print("\n" + "=" * 80)

    # Generate summary.json
    eval_end_time = datetime.now().isoformat()
    summary_data = {
        "evaluation_id": args.output_dir.name,
        "started_at": eval_start_time,
        "completed_at": eval_end_time,
        "config": {
            "model": args.model,
            "times_per_problem": args.times_per_problem,
            "parallel_requests": args.parallel_requests,
            "evaluation_mode": args.evaluation_mode,
            "difficulty": args.difficulty,
            "max_iter": args.max_iter,
            "timeout": args.timeout,
        },
        "results": {
            "total_runs": len(agent_results),
            "successful_agent_runs": successful,
            "failed_agent_runs": failed,
            "agent_success_rate": successful / len(agent_results) if agent_results else 0,
        },
        "tasks": {}
    }

    # Add task-level statistics with per-run metrics (task_run_metrics already collected earlier)
    is_flareon_mode = args.evaluation_mode == "flare-on"

    if is_flareon_mode:
        # For Flare-On: track solve rate
        total_solved = 0
        for task_id in sorted(task_results.keys()):
            stats = task_results[task_id]
            run_results = task_run_metrics.get(task_id, [])

            solved_runs = sum(1 for r in run_results if r.get("correct") == True)
            total_solved += solved_runs

            summary_data["tasks"][task_id] = {
                "runs": stats["total"],
                "completed": stats["success"],
                "failed": stats["failed"],
                "solved": solved_runs,
                "completion_rate": stats["success"] / stats["total"] if stats["total"] > 0 else 0,
                "solve_rate": solved_runs / stats["total"] if stats["total"] > 0 else 0,
                "run_results": run_results,
            }

        # Overall metrics for Flare-On
        summary_data["overall_metrics"] = {
            "total_runs": len(agent_results),
            "total_solved": total_solved,
            "solve_rate": total_solved / len(agent_results) if agent_results else 0,
        }
    else:
        # For RE mode: extract semantic similarity and correctness scores
        all_semantic_similarities = []
        all_correctness_scores = []

        for task_id in sorted(task_results.keys()):
            stats = task_results[task_id]
            run_results = task_run_metrics.get(task_id, [])

            # Extract metrics for successful runs
            task_semantic_similarities = [
                r["semantic_similarity"]
                for r in run_results
                if r["semantic_similarity"] is not None
            ]
            task_correctness_scores = [
                r["correctness_score"]
                for r in run_results
                if r["correctness_score"] is not None
            ]

            # Add to overall metrics
            all_semantic_similarities.extend(task_semantic_similarities)
            all_correctness_scores.extend(task_correctness_scores)

            summary_data["tasks"][task_id] = {
                "runs": stats["total"],
                "successful": stats["success"],
                "failed": stats["failed"],
                "success_rate": stats["success"] / stats["total"] if stats["total"] > 0 else 0,
                "run_results": run_results,
                "metrics": {
                    "semantic_similarity": calculate_statistics(task_semantic_similarities),
                    "correctness_score": calculate_statistics(task_correctness_scores),
                },
            }

        # Overall metrics for RE mode
        summary_data["overall_metrics"] = {
            "semantic_similarity": calculate_statistics(all_semantic_similarities),
            "correctness_score": calculate_statistics(all_correctness_scores),
        }

    # Add judge statistics if RE mode
    if is_re_mode and judge_results:
        judge_successful = sum(1 for _, _, success, _ in judge_results if success)
        summary_data["results"]["successful_judge_runs"] = judge_successful
        summary_data["results"]["failed_judge_runs"] = len(judge_results) - judge_successful
        summary_data["results"]["judge_success_rate"] = judge_successful / len(judge_results) if judge_results else 0

    # Save summary
    with open(eval_paths.summary_path, "w") as f:
        json.dump(summary_data, f, indent=2)
    logger.info(f"Summary saved to: {eval_paths.summary_path}")

    # Generate failed_runs.json if there are failures
    if failed > 0 or (is_re_mode and judge_results and any(not success for _, _, success, _ in judge_results)):
        failed_data = {
            "failed_agent_runs": [
                {
                    "task_id": task_id,
                    "run_number": run_num,
                    "error": error
                }
                for task_id, run_num, success, error, agent_id in agent_results
                if not success
            ],
            "failed_judge_runs": [
                {
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "error": error
                }
                for task_id, agent_id, success, error in judge_results
                if not success
            ] if is_re_mode else []
        }
        with open(eval_paths.failed_runs_path, "w") as f:
            json.dump(failed_data, f, indent=2)
        logger.info(f"Failed runs saved to: {eval_paths.failed_runs_path}")

    # Exit with error code if any runs failed
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s] %(message)s",
    )
    main()
