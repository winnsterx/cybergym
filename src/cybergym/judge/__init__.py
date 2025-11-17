"""
CyberGym Judge Module

This module provides LLM-based evaluation for reverse engineering submissions.
The judge compares agent-generated pseudocode against original source code to
assess semantic similarity and correctness.

Key Components:
- LLMJudge: Main judge class for evaluation
- run_judge_on_task: Batch evaluation for all submissions of a task
- run_judge_on_submission: Single submission evaluation

Example:
    >>> from cybergym.judge import LLMJudge
    >>> judge = LLMJudge(model="claude-sonnet-4-5-20250929")
    >>> scores = judge.evaluate(pseudocode, source_code, task_id="arvo:10400")
"""

from .judge import LLMJudge

__all__ = ["LLMJudge"]
