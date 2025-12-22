"""
Report generation for CyberGym evaluations.

Handles generation of summary.json and failed_runs.json.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .metrics import aggregate_task_metrics
from .types import AgentResult, JudgeResult

logger = logging.getLogger(__name__)


def build_task_results(agent_results: list[AgentResult]) -> dict[str, dict]:
    """
    Build task-level success/failure counts from agent results.

    Args:
        agent_results: List of agent result tuples

    Returns:
        Dict mapping task_id to {"total", "success", "failed"} counts
    """
    task_results: dict[str, dict] = {}
    for task_id, run_num, success, error, agent_id in agent_results:
        if task_id not in task_results:
            task_results[task_id] = {"total": 0, "success": 0, "failed": 0}
        task_results[task_id]["total"] += 1
        if success:
            task_results[task_id]["success"] += 1
        else:
            task_results[task_id]["failed"] += 1
    return task_results


@dataclass
class EvalConfig:
    """Configuration snapshot for evaluation reporting."""
    model: str
    times_per_problem: int
    parallel_requests: int
    evaluation_mode: str
    difficulty: str
    max_iter: int
    timeout: int
    num_of_judges: int = 1
    grading_schema: str = "five-point"


class EvalReporter:
    """Generates evaluation reports (summary.json, failed_runs.json)."""

    def __init__(
        self,
        eval_paths: Any,  # EvaluationPaths
        config: EvalConfig,
        start_time: str,
    ):
        self.eval_paths = eval_paths
        self.config = config
        self.start_time = start_time

    def generate_reports(
        self,
        agent_results: list[AgentResult],
        judge_results: list[JudgeResult],
        task_run_metrics: dict[str, list[dict]] | None = None,
    ) -> dict:
        """
        Generate all reports and return summary data.

        Args:
            agent_results: List of agent result tuples
            judge_results: List of judge result tuples
            task_run_metrics: Pre-collected metrics (if None, will be collected here)

        Returns:
            Summary data dictionary
        """
        # Build task results summary using shared function
        task_results = build_task_results(agent_results)

        # Use pre-collected metrics if provided, otherwise collect them
        if task_run_metrics is None:
            from .metrics import collect_run_metrics
            task_run_metrics = {}
            for task_id, run_num, success, error, agent_id in agent_results:
                if task_id not in task_run_metrics:
                    task_run_metrics[task_id] = []
                run_metrics = collect_run_metrics(
                    task_id=task_id,
                    run_number=run_num,
                    eval_paths=self.eval_paths,
                    agent_success=success,
                    agent_error=error,
                    evaluation_mode=self.config.evaluation_mode,
                    grading_schema=self.config.grading_schema,
                )
                task_run_metrics[task_id].append(run_metrics)

        # Generate summary
        summary_data = self._generate_summary(
            agent_results, judge_results, task_results, task_run_metrics
        )

        # Save summary
        self._save_summary(summary_data)

        # Save failed runs if any
        self._save_failed_runs(agent_results, judge_results)

        return summary_data

    def _generate_summary(
        self,
        agent_results: list[AgentResult],
        judge_results: list[JudgeResult],
        task_results: dict[str, dict],
        task_run_metrics: dict[str, list[dict]],
    ) -> dict:
        """Generate the summary data structure."""
        successful = sum(1 for _, _, success, _, _ in agent_results if success)
        failed = len(agent_results) - successful

        summary_data = {
            "evaluation_id": self.eval_paths.eval_dir.name,
            "started_at": self.start_time,
            "completed_at": datetime.now().isoformat(),
            "config": {
                "model": self.config.model,
                "times_per_problem": self.config.times_per_problem,
                "parallel_requests": self.config.parallel_requests,
                "evaluation_mode": self.config.evaluation_mode,
                "difficulty": self.config.difficulty,
                "max_iter": self.config.max_iter,
                "timeout": self.config.timeout,
            },
            "results": {
                "total_runs": len(agent_results),
                "successful_agent_runs": successful,
                "failed_agent_runs": failed,
                "agent_success_rate": successful / len(agent_results) if agent_results else 0,
            },
            "tasks": {},
        }

        # Add mode-specific metrics
        is_ctf_mode = self.config.evaluation_mode == "ctf"
        is_re_mode = self.config.evaluation_mode == "pseudocode"
        is_exploit_mode = self.config.evaluation_mode in ("exploit", "exploit_library_binary", "exploit_fuzzer_binary")

        if is_ctf_mode:
            self._add_ctf_metrics(summary_data, task_results, task_run_metrics)
        elif is_exploit_mode:
            self._add_exploit_metrics(summary_data, task_results, task_run_metrics)
        else:
            self._add_re_metrics(summary_data, task_results, task_run_metrics)

        # Add judge statistics if RE mode
        if is_re_mode and judge_results:
            judge_successful = sum(1 for _, _, _, success, _ in judge_results if success)
            summary_data["results"]["successful_judge_runs"] = judge_successful
            summary_data["results"]["failed_judge_runs"] = len(judge_results) - judge_successful
            summary_data["results"]["judge_success_rate"] = (
                judge_successful / len(judge_results) if judge_results else 0
            )
            summary_data["config"]["num_of_judges"] = self.config.num_of_judges

        return summary_data

    def _add_ctf_metrics(
        self,
        summary_data: dict,
        task_results: dict[str, dict],
        task_run_metrics: dict[str, list[dict]],
    ):
        """Add CTF-specific metrics to summary."""
        per_task_metrics, overall_metrics = aggregate_task_metrics(
            task_run_metrics, "ctf"
        )

        for task_id in sorted(task_results.keys()):
            stats = task_results[task_id]
            run_results = task_run_metrics.get(task_id, [])
            task_metrics = per_task_metrics.get(task_id, {})

            summary_data["tasks"][task_id] = {
                "runs": stats["total"],
                "completed": stats["success"],
                "failed": stats["failed"],
                "solved": task_metrics.get("solved", 0),
                "completion_rate": stats["success"] / stats["total"] if stats["total"] > 0 else 0,
                "solve_rate": task_metrics.get("solve_rate", 0),
                "run_results": run_results,
            }

        summary_data["overall_metrics"] = overall_metrics

    def _add_exploit_metrics(
        self,
        summary_data: dict,
        task_results: dict[str, dict],
        task_run_metrics: dict[str, list[dict]],
    ):
        """Add exploit/exploit_library_binary-specific metrics to summary.

        For exploit modes, we distinguish between:
        - completed: agent ran without errors
        - success: agent found a valid exploit (POC triggered crash)
        """
        per_task_metrics, overall_metrics = aggregate_task_metrics(
            task_run_metrics, self.config.evaluation_mode
        )

        # Update results section with exploit-specific metrics
        summary_data["results"] = {
            "total_runs": overall_metrics.get("total_runs", 0),
            "completed_runs": overall_metrics.get("total_completed", 0),
            "successful_runs": overall_metrics.get("total_successful", 0),
            "run_success_rate": overall_metrics.get("run_success_rate", 0),
            "successful_tasks": overall_metrics.get("successful_tasks", 0),
            "task_success_rate": overall_metrics.get("task_success_rate", 0),
        }

        for task_id in sorted(task_results.keys()):
            run_results = task_run_metrics.get(task_id, [])
            task_metrics = per_task_metrics.get(task_id, {})

            # Count completed and successful runs from run_results
            completed = sum(1 for r in run_results if r.get("status") in ("completed", "success"))
            successful = sum(1 for r in run_results if r.get("exploit_success") is True)

            summary_data["tasks"][task_id] = {
                "runs": len(run_results),
                "completed": completed,
                "successful": successful,
                "success_rate": task_metrics.get("success_rate", 0),
                "total_poc_submissions": task_metrics.get("total_poc_submissions", 0),
                "total_successful_pocs": task_metrics.get("total_successful_pocs", 0),
                "run_results": run_results,
            }

        summary_data["overall_metrics"] = overall_metrics

    def _add_re_metrics(
        self,
        summary_data: dict,
        task_results: dict[str, dict],
        task_run_metrics: dict[str, list[dict]],
    ):
        """Add RE-specific metrics to summary."""
        per_task_metrics, overall_metrics = aggregate_task_metrics(
            task_run_metrics, "pseudocode"
        )

        for task_id in sorted(task_results.keys()):
            stats = task_results[task_id]
            run_results = task_run_metrics.get(task_id, [])
            task_metrics = per_task_metrics.get(task_id, {})

            summary_data["tasks"][task_id] = {
                "runs": stats["total"],
                "successful": stats["success"],
                "failed": stats["failed"],
                "success_rate": stats["success"] / stats["total"] if stats["total"] > 0 else 0,
                "run_results": run_results,
                "metrics": task_metrics,
            }

        summary_data["overall_metrics"] = overall_metrics
        summary_data["grading_schema"] = self.config.grading_schema

    def _save_summary(self, summary_data: dict):
        """Save summary.json."""
        with open(self.eval_paths.summary_path, "w") as f:
            json.dump(summary_data, f, indent=2)
        logger.info(f"Summary saved to: {self.eval_paths.summary_path}")

    def _save_failed_runs(
        self,
        agent_results: list[AgentResult],
        judge_results: list[JudgeResult],
    ):
        """Save failed_runs.json if there are failures."""
        is_re_mode = self.config.evaluation_mode == "pseudocode"

        failed_agents = [
            {"task_id": task_id, "run_number": run_num, "error": error}
            for task_id, run_num, success, error, agent_id in agent_results
            if not success
        ]

        failed_judges = []
        if is_re_mode:
            failed_judges = [
                {"task_id": task_id, "agent_id": agent_id, "judge_number": judge_num, "error": error}
                for task_id, agent_id, judge_num, success, error in judge_results
                if not success
            ]

        if failed_agents or failed_judges:
            failed_data = {
                "failed_agent_runs": failed_agents,
                "failed_judge_runs": failed_judges,
            }
            with open(self.eval_paths.failed_runs_path, "w") as f:
                json.dump(failed_data, f, indent=2)
            logger.info(f"Failed runs saved to: {self.eval_paths.failed_runs_path}")


def print_evaluation_summary(
    agent_results: list[AgentResult],
    judge_results: list[JudgeResult],
    task_run_metrics: dict[str, list[dict]],
    eval_paths: Any,
    elapsed_time: float,
    evaluation_mode: str,
):
    """
    Print evaluation summary to console.

    Args:
        agent_results: List of agent result tuples
        judge_results: List of judge result tuples
        task_run_metrics: Dict mapping task_id to list of run metrics
        eval_paths: EvaluationPaths instance
        elapsed_time: Time elapsed in seconds
        evaluation_mode: Evaluation mode
    """
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

    is_re_mode = evaluation_mode == "pseudocode"
    is_ctf_mode = evaluation_mode == "ctf"
    is_exploit_mode = evaluation_mode in ("exploit", "exploit_library_binary", "exploit_fuzzer_binary")

    # Print exploit-specific summary
    if is_exploit_mode:
        exploit_success = sum(
            1 for r in sum(task_run_metrics.values(), [])
            if r.get("exploit_success") is True
        )
        print(f"\nExploit Results:")
        print(f"  Successful exploits: {exploit_success}/{len(agent_results)}")
        print(f"  Exploit success rate: {exploit_success / len(agent_results) * 100:.1f}%")

    # Print judge summary if RE mode
    if is_re_mode and judge_results:
        judge_successful = sum(1 for _, _, _, success, _ in judge_results if success)
        judge_failed = len(judge_results) - judge_successful
        print(f"\nJudge Evaluations:")
        print(f"  Total: {len(judge_results)}")
        print(f"  Successful: {judge_successful}")
        print(f"  Failed: {judge_failed}")
        if judge_results:
            print(f"  Success rate: {judge_successful / len(judge_results) * 100:.1f}%")

    print("=" * 80)

    # Build task results using shared function
    task_results = build_task_results(agent_results)

    print("\nPer-task results:")
    for task_id in sorted(task_results.keys()):
        stats = task_results[task_id]
        if is_ctf_mode:
            run_results = task_run_metrics.get(task_id, [])
            solved = sum(1 for r in run_results if r.get("correct") is True)
            completion_rate = stats["success"] / stats["total"] * 100
            solve_rate = solved / stats["total"] * 100
            print(
                f"  {task_id}: {stats['success']}/{stats['total']} completed ({completion_rate:.1f}%), "
                f"{solved}/{stats['total']} solved ({solve_rate:.1f}%)"
            )
        elif is_exploit_mode:
            run_results = task_run_metrics.get(task_id, [])
            exploit_success = sum(1 for r in run_results if r.get("exploit_success") is True)
            completion_rate = stats["success"] / stats["total"] * 100
            success_rate = exploit_success / stats["total"] * 100
            print(
                f"  {task_id}: {stats['success']}/{stats['total']} completed ({completion_rate:.1f}%), "
                f"{exploit_success}/{stats['total']} exploited ({success_rate:.1f}%)"
            )
        else:
            success_rate = stats["success"] / stats["total"] * 100
            print(f"  {task_id}: {stats['success']}/{stats['total']} successful ({success_rate:.1f}%)")

    # Print failed agent runs
    if failed > 0:
        print("\nFailed agent runs:")
        for task_id, run_num, success, error, agent_id in agent_results:
            if not success:
                print(f"  ✗ {task_id} run {run_num}: {error}")

    # Print failed judge runs
    if is_re_mode and judge_results:
        judge_failed_list = [
            (task_id, agent_id, judge_num, error)
            for task_id, agent_id, judge_num, success, error in judge_results
            if not success
        ]
        if judge_failed_list:
            print("\nFailed judge evaluations:")
            for task_id, agent_id, judge_num, error in judge_failed_list:
                print(f"  ✗ {task_id} agent {agent_id} judge {judge_num}: {error}")

    # Print overall solve rate for CTF mode
    if is_ctf_mode:
        total_solved = sum(
            sum(1 for r in task_run_metrics.get(tid, []) if r.get("correct") is True)
            for tid in task_results.keys()
        )
        solve_rate = total_solved / len(agent_results) * 100 if agent_results else 0
        print(f"\nOverall Solve Rate: {total_solved}/{len(agent_results)} ({solve_rate:.1f}%)")

    # Print overall exploit success rate for exploit modes
    if is_exploit_mode:
        total_exploited = sum(
            sum(1 for r in task_run_metrics.get(tid, []) if r.get("exploit_success") is True)
            for tid in task_results.keys()
        )
        tasks_exploited = sum(
            1 for tid in task_results.keys()
            if any(r.get("exploit_success") is True for r in task_run_metrics.get(tid, []))
        )
        exploit_rate = total_exploited / len(agent_results) * 100 if agent_results else 0
        task_rate = tasks_exploited / len(task_results) * 100 if task_results else 0
        print(f"\nOverall Exploit Rate: {total_exploited}/{len(agent_results)} runs ({exploit_rate:.1f}%)")
        print(f"Tasks with at least one exploit: {tasks_exploited}/{len(task_results)} ({task_rate:.1f}%)")

    print("\n" + "=" * 80)
