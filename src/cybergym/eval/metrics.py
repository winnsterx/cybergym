"""
Metrics collection and statistics calculation for CyberGym evaluations.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def extract_telemetry_from_trajectory(trajectory_path: Path) -> dict:
    """
    Extract token usage and timing from an OpenHands trajectory file.

    Args:
        trajectory_path: Path to the trajectory JSON file

    Returns:
        Dictionary with token counts and timing info
    """
    if not trajectory_path.exists():
        return {"error": "trajectory not found"}

    try:
        with open(trajectory_path) as f:
            events = json.load(f)
    except Exception as e:
        return {"error": f"failed to parse trajectory: {e}"}

    if not events:
        return {"error": "empty trajectory"}

    # Token aggregation
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_cache_read_tokens = 0
    total_cache_write_tokens = 0
    llm_call_count = 0

    for event in events:
        tcm = event.get("tool_call_metadata")
        if tcm and isinstance(tcm, dict):
            mr = tcm.get("model_response")
            if mr and isinstance(mr, dict):
                usage = mr.get("usage", {})
                if usage:
                    total_prompt_tokens += usage.get("prompt_tokens", 0)
                    total_completion_tokens += usage.get("completion_tokens", 0)
                    total_cache_read_tokens += usage.get("cache_read_input_tokens", 0)
                    total_cache_write_tokens += usage.get("cache_creation_input_tokens", 0)
                    llm_call_count += 1

    # Time extraction
    start_time = events[0].get("timestamp") if events else None
    end_time = events[-1].get("timestamp") if events else None

    duration_seconds = None
    if start_time and end_time:
        try:
            start_dt = datetime.fromisoformat(start_time)
            end_dt = datetime.fromisoformat(end_time)
            duration_seconds = (end_dt - start_dt).total_seconds()
        except Exception:
            pass

    return {
        "tokens": {
            "prompt_tokens": total_prompt_tokens,
            "completion_tokens": total_completion_tokens,
            "total_tokens": total_prompt_tokens + total_completion_tokens,
            "cache_read_tokens": total_cache_read_tokens,
            "cache_write_tokens": total_cache_write_tokens,
            "llm_calls": llm_call_count,
        },
        "timing": {
            "start_time": start_time,
            "end_time": end_time,
            "duration_seconds": duration_seconds,
        },
    }


def calculate_statistics(values: list[float]) -> dict[str, float | None]:
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
    eval_paths: Any,  # EvaluationPaths
    agent_success: bool,
    agent_error: str | None,
    evaluation_mode: str = "reverse_engineering",
    grading_schema: str = "five-point",
    server_url: str | None = None,
    agent_id: str | None = None,
) -> dict:
    """
    Collect metrics for a single run from the evaluation.json file or database.

    Args:
        task_id: Task identifier
        run_number: Run number
        eval_paths: EvaluationPaths instance
        agent_success: Whether the agent run succeeded
        agent_error: Error message if agent failed
        evaluation_mode: Evaluation mode (reverse_engineering, ctf, exploit)
        grading_schema: Grading schema name (for RE mode)
        server_url: Optional server URL for querying submissions via HTTP (Modal runtime)
        agent_id: Optional agent ID for filtering submissions

    Returns:
        Dictionary with run_id, status, and metrics.
        For RE mode, includes 'evaluations' list with all judge evaluations.
        For CTF mode, includes 'correct' boolean.
        Always includes 'telemetry' with token counts and timing.
    """
    # Base result for all modes
    result = {
        "run_id": run_number,
        "status": "success" if agent_success else "failed",
    }

    # Extract telemetry (tokens + timing) from trajectory
    # Note: trajectory is a file, not a directory (despite the method name)
    trajectory_path = eval_paths.agent_dir(task_id, run_number) / "trajectory"
    telemetry = extract_telemetry_from_trajectory(trajectory_path)
    result["telemetry"] = telemetry

    # For CTF mode, only track correct/incorrect
    if evaluation_mode == "ctf":
        return _collect_ctf_metrics(task_id, run_number, eval_paths, agent_success, agent_error, result, server_url, agent_id)

    # For RE mode, load all judge evaluations from database
    if not agent_success:
        result["error"] = agent_error
        result["evaluations"] = []
        return result

    return _collect_re_metrics(task_id, run_number, eval_paths, result, server_url, agent_id)


def _collect_ctf_metrics(
    task_id: str,
    run_number: int,
    eval_paths: Any,
    agent_success: bool,
    agent_error: str | None,
    result: dict,
    server_url: str | None = None,
    agent_id: str | None = None,
) -> dict:
    """Collect metrics for CTF mode."""
    from .client import get_submission_client

    if not agent_success:
        result["error"] = agent_error
        result["correct"] = False
        return result

    try:
        client = get_submission_client(
            server_url=server_url,
            db_path=eval_paths.database_path if not server_url else None,
        )

        # Check for correct submissions
        correct_subs = client.get_ctf_submissions(task_id, agent_id, correct=1)
        if correct_subs:
            result["correct"] = True
            return result

        # Check for any submissions
        all_subs = client.get_ctf_submissions(task_id, agent_id)
        if all_subs:
            result["correct"] = False
        else:
            result["error"] = "No submissions found"
            result["correct"] = False

    except Exception as e:
        logger.warning(f"Failed to query CTF submissions for {task_id} run {run_number}: {e}")
        result["error"] = f"Failed to query submissions: {str(e)}"
        result["correct"] = False

    return result


def _collect_re_metrics(
    task_id: str,
    run_number: int,
    eval_paths: Any,
    result: dict,
    server_url: str | None = None,
    agent_id: str | None = None,
) -> dict:
    """Collect metrics for RE mode."""
    from .client import get_submission_client

    try:
        client = get_submission_client(
            server_url=server_url,
            db_path=eval_paths.database_path if not server_url else None,
        )

        submission = client.get_re_submission(task_id, agent_id)
        if submission and submission.evaluations:
            result["evaluations"] = submission.evaluations
            return result

    except Exception as e:
        logger.warning(f"Failed to query RE submissions for {task_id} run {run_number}: {e}")

    # If no evaluations found, return empty list
    result["evaluations"] = []
    return result


def _aggregate_telemetry(task_run_metrics: dict[str, list[dict]]) -> dict:
    """
    Aggregate telemetry (tokens + timing) across all runs.

    Returns:
        Dictionary with total tokens and timing statistics.
    """
    all_prompt_tokens = []
    all_completion_tokens = []
    all_total_tokens = []
    all_durations = []
    total_prompt = 0
    total_completion = 0
    total_llm_calls = 0

    for task_id, run_results in task_run_metrics.items():
        for run_result in run_results:
            telemetry = run_result.get("telemetry", {})
            if "error" in telemetry:
                continue

            tokens = telemetry.get("tokens", {})
            timing = telemetry.get("timing", {})

            prompt = tokens.get("prompt_tokens", 0)
            completion = tokens.get("completion_tokens", 0)
            total = tokens.get("total_tokens", 0)
            duration = timing.get("duration_seconds")

            if prompt > 0:
                all_prompt_tokens.append(prompt)
                total_prompt += prompt
            if completion > 0:
                all_completion_tokens.append(completion)
                total_completion += completion
            if total > 0:
                all_total_tokens.append(total)
            if duration is not None:
                all_durations.append(duration)
            total_llm_calls += tokens.get("llm_calls", 0)

    return {
        "totals": {
            "prompt_tokens": total_prompt,
            "completion_tokens": total_completion,
            "total_tokens": total_prompt + total_completion,
            "llm_calls": total_llm_calls,
        },
        "per_run_stats": {
            "prompt_tokens": calculate_statistics(all_prompt_tokens),
            "completion_tokens": calculate_statistics(all_completion_tokens),
            "total_tokens": calculate_statistics(all_total_tokens),
            "duration_seconds": calculate_statistics(all_durations),
        },
    }


def aggregate_task_metrics(
    task_run_metrics: dict[str, list[dict]],
    evaluation_mode: str,
) -> tuple[dict[str, dict], dict[str, Any]]:
    """
    Aggregate metrics across all runs for summary generation.

    Args:
        task_run_metrics: Dict mapping task_id to list of run metrics
        evaluation_mode: Evaluation mode (reverse_engineering, ctf, exploit)

    Returns:
        Tuple of (per_task_metrics, overall_metrics)
    """
    # Aggregate telemetry for all modes
    telemetry_stats = _aggregate_telemetry(task_run_metrics)

    if evaluation_mode == "ctf":
        per_task, overall = _aggregate_ctf_metrics(task_run_metrics)
    else:
        per_task, overall = _aggregate_re_metrics(task_run_metrics)

    # Add telemetry to overall metrics
    overall["telemetry"] = telemetry_stats
    return per_task, overall


def _aggregate_ctf_metrics(
    task_run_metrics: dict[str, list[dict]],
) -> tuple[dict[str, dict], dict[str, Any]]:
    """Aggregate CTF metrics."""
    per_task = {}
    total_solved = 0
    total_runs = 0

    for task_id, run_results in task_run_metrics.items():
        solved_runs = sum(1 for r in run_results if r.get("correct") is True)
        total_solved += solved_runs
        total_runs += len(run_results)

        per_task[task_id] = {
            "solved": solved_runs,
            "solve_rate": solved_runs / len(run_results) if run_results else 0,
        }

    overall = {
        "total_runs": total_runs,
        "total_solved": total_solved,
        "solve_rate": total_solved / total_runs if total_runs else 0,
    }

    return per_task, overall


def _aggregate_re_metrics(
    task_run_metrics: dict[str, list[dict]],
) -> tuple[dict[str, dict], dict[str, Any]]:
    """Aggregate RE metrics across all judge evaluations."""
    per_task = {}
    all_category_scores: dict[str, list[float]] = {}

    for task_id, run_results in task_run_metrics.items():
        # Collect scores by category across all runs and all judge evaluations
        task_category_scores: dict[str, list[float]] = {}

        for run_result in run_results:
            evaluations = run_result.get("evaluations", [])
            for evaluation in evaluations:
                category_scores = evaluation.get("category_scores", {})
                for category, score in category_scores.items():
                    if category not in task_category_scores:
                        task_category_scores[category] = []
                    task_category_scores[category].append(score)

        # Add to overall metrics
        for category, scores in task_category_scores.items():
            if category not in all_category_scores:
                all_category_scores[category] = []
            all_category_scores[category].extend(scores)

        # Compute statistics for this task
        task_metrics = {}
        for category, scores in task_category_scores.items():
            task_metrics[category] = calculate_statistics(scores)

        per_task[task_id] = task_metrics

    # Overall metrics aggregated across all tasks and judges
    overall = {}
    for category, scores in all_category_scores.items():
        overall[category] = calculate_statistics(scores)

    return per_task, overall
