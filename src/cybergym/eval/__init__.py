"""CyberGym evaluation utilities."""

from .paths import EvaluationPaths, LegacyPaths, get_evaluation_paths
from .judge_parser import parse_judge_evaluation, list_schemas
from .metrics import calculate_statistics, collect_run_metrics, aggregate_task_metrics
from .orchestrator import run_evaluation_pool
from .reporter import EvalConfig, EvalReporter, print_evaluation_summary, build_task_results
from .types import AgentResult, JudgeResult
from .client import SubmissionClient, RESubmissionResult, CTFSubmissionResult, get_submission_client

__all__ = [
    # Types
    "AgentResult",
    "JudgeResult",
    "RESubmissionResult",
    "CTFSubmissionResult",
    # Paths
    "EvaluationPaths",
    "LegacyPaths",
    "get_evaluation_paths",
    # Client
    "SubmissionClient",
    "get_submission_client",
    # Judge parsing
    "parse_judge_evaluation",
    "list_schemas",
    # Metrics
    "calculate_statistics",
    "collect_run_metrics",
    "aggregate_task_metrics",
    # Orchestration
    "run_evaluation_pool",
    # Reporting
    "EvalConfig",
    "EvalReporter",
    "print_evaluation_summary",
    "build_task_results",
]
