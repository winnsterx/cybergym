"""Shared type definitions for CyberGym evaluation."""

# Type aliases for result tuples
AgentResult = tuple[str, int, bool, str | None, str | None]
"""Agent result: (task_id, run_num, success, error, agent_id)"""

JudgeResult = tuple[str, str, int, bool, str | None]
"""Judge result: (task_id, agent_id, judge_num, success, error)"""
