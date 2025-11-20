"""
Centralized path management for CyberGym evaluation runs.

This module provides a consistent interface for creating and accessing
evaluation directory structures, eliminating hardcoded paths throughout the codebase.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class EvaluationPaths:
    """
    Manages all paths for a CyberGym evaluation run.

    Directory structure:
        eval_dir/
        ├── runs/
        │   └── {task_id}/
        │       └── run_{N}/
        │           ├── agent/
        │           │   ├── metadata.json
        │           │   ├── workspace/
        │           │   ├── trajectory/
        │           │   ├── logs/
        │           │   ├── cache/
        │           │   └── file/
        │           └── judge/
        │               ├── metadata.json
        │               ├── evaluation.json
        │               ├── workspace/
        │               └── logs/
        ├── database/
        │   └── submissions.db
        ├── summary.json
        └── failed_runs.json
    """

    eval_dir: Path
    """Root evaluation directory (e.g., cybergym_eval_7)"""

    keep_tmp: bool = False
    """Whether to preserve temporary files for debugging"""

    server_db_path: Optional[Path] = None
    """Path to server database (if using external server DB instead of eval-specific DB)"""

    _tmp_base: Optional[Path] = None
    """Base directory for temporary files"""

    def __post_init__(self):
        """Ensure paths are absolute."""
        self.eval_dir = self.eval_dir.absolute()
        if self.server_db_path is not None:
            self.server_db_path = self.server_db_path.absolute()

    # ===== Root-level paths =====

    @property
    def runs_dir(self) -> Path:
        """Directory containing all task runs."""
        return self.eval_dir / "runs"

    @property
    def database_dir(self) -> Path:
        """Directory containing evaluation databases."""
        return self.eval_dir / "database"

    @property
    def database_path(self) -> Path:
        """
        Path to submissions database.

        Always uses the server database (server_poc/poc.db).
        If server_db_path is explicitly set, use that path.
        Otherwise, look for server_poc/poc.db in the current working directory.
        """
        if self.server_db_path is not None:
            return self.server_db_path

        # Default to server_poc/poc.db in current working directory
        default_server_db = Path.cwd() / "server_poc" / "poc.db"
        return default_server_db

    @property
    def summary_path(self) -> Path:
        """Path to evaluation summary JSON."""
        return self.eval_dir / "summary.json"

    @property
    def failed_runs_path(self) -> Path:
        """Path to failed runs tracking JSON."""
        return self.eval_dir / "failed_runs.json"

    # ===== Task-level paths =====

    def task_dir(self, task_id: str) -> Path:
        """Directory for a specific task."""
        safe_task_id = self._sanitize_task_id(task_id)
        return self.runs_dir / safe_task_id

    def run_dir(self, task_id: str, run_number: int) -> Path:
        """Directory for a specific run of a task."""
        return self.task_dir(task_id) / f"run_{run_number}"

    # ===== Agent paths =====

    def agent_dir(self, task_id: str, run_number: int) -> Path:
        """Directory for agent execution."""
        return self.run_dir(task_id, run_number) / "agent"

    def agent_metadata_path(self, task_id: str, run_number: int) -> Path:
        """Path to agent metadata JSON."""
        return self.agent_dir(task_id, run_number) / "metadata.json"

    def agent_workspace_dir(self, task_id: str, run_number: int) -> Path:
        """Agent's working directory."""
        return self.agent_dir(task_id, run_number) / "workspace"

    def agent_trajectory_dir(self, task_id: str, run_number: int) -> Path:
        """Agent trajectory directory."""
        return self.agent_dir(task_id, run_number) / "trajectory"

    def agent_logs_dir(self, task_id: str, run_number: int) -> Path:
        """Agent logs directory."""
        return self.agent_dir(task_id, run_number) / "logs"

    def agent_cache_dir(self, task_id: str, run_number: int) -> Path:
        """Agent cache directory."""
        return self.agent_dir(task_id, run_number) / "cache"

    def agent_file_dir(self, task_id: str, run_number: int) -> Path:
        """Agent file store directory."""
        return self.agent_dir(task_id, run_number) / "file"

    def agent_debug_dir(self, task_id: str, run_number: int) -> Path:
        """Agent debug directory (only if keep_tmp=True)."""
        return self.agent_dir(task_id, run_number) / ".debug"

    # ===== Judge paths =====

    def judge_dir(self, task_id: str, run_number: int) -> Path:
        """Directory for judge evaluation."""
        return self.run_dir(task_id, run_number) / "judge"

    def judge_metadata_path(self, task_id: str, run_number: int) -> Path:
        """Path to judge metadata JSON."""
        return self.judge_dir(task_id, run_number) / "metadata.json"

    def judge_evaluation_path(self, task_id: str, run_number: int) -> Path:
        """Path to judge evaluation results JSON."""
        return self.judge_dir(task_id, run_number) / "evaluation.json"

    def judge_workspace_dir(self, task_id: str, run_number: int) -> Path:
        """Judge working directory."""
        return self.judge_dir(task_id, run_number) / "workspace"

    def judge_logs_dir(self, task_id: str, run_number: int) -> Path:
        """Judge logs directory."""
        return self.judge_dir(task_id, run_number) / "logs"

    def judge_cache_dir(self, task_id: str, run_number: int) -> Path:
        """Judge cache directory."""
        return self.judge_dir(task_id, run_number) / "cache"

    def judge_file_dir(self, task_id: str, run_number: int) -> Path:
        """Judge file store directory."""
        return self.judge_dir(task_id, run_number) / "file"

    def judge_trajectory_dir(self, task_id: str, run_number: int) -> Path:
        """Judge trajectory directory."""
        return self.judge_dir(task_id, run_number) / "trajectory"

    # ===== Temporary file paths =====

    @property
    def tmp_base(self) -> Path:
        """
        Base directory for temporary files.

        Always uses eval_dir/.tmp/<pid>_<random>/ for temporary files.
        This ensures Docker can mount these directories (unlike system /tmp on some systems).
        Automatically cleaned up unless keep_tmp=True.
        """
        if self._tmp_base is None:
            # Always use eval_dir/.tmp instead of system /tmp
            # This is required for Docker volume mounting to work properly
            # (Docker cannot mount from /tmp on some systems)
            pid = os.getpid()
            import secrets
            random_suffix = secrets.token_hex(4)
            self._tmp_base = self.eval_dir / ".tmp" / f"{pid}_{random_suffix}"
            self._tmp_base.mkdir(parents=True, exist_ok=True)

        return self._tmp_base

    def tmp_run_dir(self, task_id: str, run_number: int, agent_id: str) -> Path:
        """Temporary directory for a specific run."""
        safe_task_id = self._sanitize_task_id(task_id)
        run_id = f"run_{safe_task_id}_{run_number}_{agent_id[:8]}"
        return self.tmp_base / run_id

    def tmp_template_dir(self, task_id: str, run_number: int, agent_id: str) -> Path:
        """Temporary template directory."""
        return self.tmp_run_dir(task_id, run_number, agent_id) / "template"

    def tmp_workspace_dir(self, task_id: str, run_number: int, agent_id: str) -> Path:
        """Temporary initial workspace directory."""
        return self.tmp_run_dir(task_id, run_number, agent_id) / "workspace"

    # ===== Helper methods =====

    @staticmethod
    def _sanitize_task_id(task_id: str) -> str:
        """Convert task_id to a valid directory name."""
        return task_id.replace(":", "_")

    def create_directory_structure(self):
        """Create the base directory structure for evaluation."""
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        # Note: We don't create database_dir anymore since we use the server's database

    def cleanup_tmp(self):
        """Clean up temporary files if not in debug mode."""
        if not self.keep_tmp and self._tmp_base and self._tmp_base.exists():
            import shutil
            shutil.rmtree(self._tmp_base, ignore_errors=True)


@dataclass
class LegacyPaths:
    """
    Handles legacy database path lookups for backward compatibility.

    Searches for databases in old locations:
    - ./server_poc/poc.db
    - ./poc.db
    """

    @staticmethod
    def find_legacy_database() -> Optional[Path]:
        """
        Search for database in legacy locations.

        Returns:
            Path to existing legacy database, or None if not found.
        """
        candidates = [
            Path.cwd() / "server_poc" / "poc.db",
            Path.cwd() / "poc.db",
        ]

        for candidate in candidates:
            if candidate.exists():
                return candidate

        return None

    @staticmethod
    def migrate_legacy_database(legacy_path: Path, new_path: Path):
        """
        Copy legacy database to new location.

        Args:
            legacy_path: Path to existing legacy database
            new_path: Path to new database location
        """
        import shutil

        new_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy_path, new_path)


def get_evaluation_paths(
    eval_dir: Path,
    keep_tmp: bool = False,
    server_db_path: Optional[Path] = None
) -> EvaluationPaths:
    """
    Create EvaluationPaths instance.

    Args:
        eval_dir: Root evaluation directory
        keep_tmp: Whether to preserve temporary files
        server_db_path: Optional override path to server database (default: ./server_poc/poc.db)

    Returns:
        Configured EvaluationPaths instance
    """
    paths = EvaluationPaths(eval_dir=eval_dir, keep_tmp=keep_tmp, server_db_path=server_db_path)
    paths.create_directory_structure()

    # Validate that the server database exists
    if not paths.database_path.exists():
        import logging
        logger = logging.getLogger(__name__)
        logger.warning(
            f"Server database not found at {paths.database_path}. "
            "Make sure the CyberGym server has been started and is running."
        )

    return paths
