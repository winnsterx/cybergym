# CyberGym Evaluation Directory Restructuring

## Overview

This document describes the comprehensive restructuring of the CyberGym evaluation directory structure to improve organization, readability, and maintainability.

## Key Changes

### 1. New Directory Structure

**Before:**
```
cybergym_eval_7/
├── arvo_47101/
│   ├── logs/
│   │   └── run_0/
│   │       └── arvo_47101-{agent_id}/
│   │           ├── args.json
│   │           └── logs/
│   │               └── workspace/
│   ├── tmp/
│   │   └── run_0/                    # Removed after completion
│   └── (no judge structure)
└── server_poc/poc.db                 # Inconsistent location
```

**After:**
```
cybergym_eval_7/
├── runs/                             # All task runs
│   └── arvo_47101/
│       └── run_0/
│           ├── agent/                # Agent execution
│           │   ├── metadata.json     # Comprehensive metadata
│           │   ├── workspace/        # Agent workspace
│           │   ├── trajectory/       # Agent trajectory
│           │   ├── logs/             # Execution logs
│           │   ├── cache/            # Cache directory
│           │   └── file/             # File store
│           └── judge/                # Judge evaluation
│               ├── metadata.json
│               ├── evaluation.json   # Evaluation scores
│               ├── workspace/
│               ├── trajectory/
│               └── logs/
├── database/
│   └── submissions.db                # Centralized database
├── summary.json                      # Evaluation summary
└── failed_runs.json                  # Failed runs tracking

/tmp/cybergym_eval_7_{pid}/           # Temporary files (auto-cleaned)
└── run_{task}_{number}_{agent_id}/
    ├── template/
    └── workspace/
```

### 2. Centralized Path Management

Created `src/cybergym/eval/paths.py` with `EvaluationPaths` class:

- **Eliminates hardcoded paths** throughout the codebase
- **Single source of truth** for all directory structures
- **Easy to modify** - change paths in one place
- **Type-safe** path construction with methods like:
  - `agent_dir(task_id, run_number)`
  - `judge_dir(task_id, run_number)`
  - `agent_workspace_dir(task_id, run_number)`
  - `database_path`
  - `tmp_run_dir(task_id, run_number, agent_id)`

### 3. Temporary File Management

**Before:**
- Tmp files stored in `{task}/tmp/run_N/` within output directory
- Cluttered output directory
- Manual cleanup needed

**After:**
- Tmp files in system `/tmp/cybergym_*` directory
- Automatic cleanup on exit
- Optional `--keep-tmp` flag stores in `.debug/` subdirectory
- Cleaner output directory

### 4. Database Consolidation

**Before:**
- Database at `./server_poc/poc.db` or `./poc.db`
- Inconsistent location
- Not part of evaluation output

**After:**
- Database at `{eval_dir}/database/submissions.db`
- Part of evaluation output
- Automatic migration from legacy locations
- Consistent across all evaluations

### 5. Metadata Improvements

**Before:**
- Single `args.json` with basic info
- No evaluation-level summary
- No failed runs tracking

**After:**

**metadata.json** (per run):
```json
{
  "agent": "openhands:claude-sonnet-4-5-20250929",
  "agent_id": "abc123def456",
  "task": {...},
  "agent_args": {...},
  "task_args": {...}
}
```

**summary.json** (evaluation-level):
```json
{
  "evaluation_id": "cybergym_eval_7",
  "started_at": "2025-01-18T10:00:00Z",
  "completed_at": "2025-01-18T12:00:00Z",
  "config": {
    "model": "claude-sonnet-4-5-20250929",
    "times_per_problem": 1,
    "parallel_requests": 1,
    "evaluation_mode": "reverse_engineering"
  },
  "results": {
    "total_runs": 2,
    "successful_agent_runs": 2,
    "failed_agent_runs": 0,
    "agent_success_rate": 1.0,
    "successful_judge_runs": 2,
    "failed_judge_runs": 0
  },
  "tasks": {
    "arvo:47101": {
      "runs": 1,
      "successful": 1,
      "failed": 0,
      "success_rate": 1.0
    }
  }
}
```

**failed_runs.json** (if failures occur):
```json
{
  "failed_agent_runs": [
    {
      "task_id": "arvo:47101",
      "run_number": 0,
      "error": "Validation error"
    }
  ],
  "failed_judge_runs": [...]
}
```

### 6. Debug Mode Support

New `--keep-tmp` flag:

**Without flag (default):**
- Tmp files in system `/tmp/`, auto-cleaned
- Clean output directory

**With `--keep-tmp`:**
- Tmp files copied to `runs/task/run_N/agent/.debug/`
- Includes initial workspace state, config files
- Useful for debugging agent issues

### 7. Co-located Judge Results

**Before:**
- Agent results in `logs/run_0/task-{agent_id}/`
- Judge results in separate `logs/judge_workspaces/judge_{submission_id}/`
- Hard to correlate agent run with judge evaluation

**After:**
- Agent results in `runs/task/run_0/agent/`
- Judge results in `runs/task/run_0/judge/`
- Clear parent-child relationship
- Easy to find judge results for specific agent run

## Code Changes

### New Files

1. **`src/cybergym/eval/paths.py`**
   - `EvaluationPaths` class for path management
   - `LegacyPaths` class for backward compatibility
   - `get_evaluation_paths()` helper function

2. **`src/cybergym/eval/__init__.py`**
   - Package initialization
   - Exports for easy importing

3. **`RESTRUCTURING_CHANGES.md`** (this file)
   - Comprehensive documentation of changes

### Modified Files

1. **`run_eval.py`**
   - Uses `EvaluationPaths` instead of hardcoded paths
   - Adds `--keep-tmp` flag
   - Generates `summary.json` and `failed_runs.json`
   - Updated all function signatures to pass `eval_paths` and `run_number`
   - Removed obsolete `task_dirs` dictionary
   - Uses centralized database path

2. **`examples/agents/openhands/run.py`**
   - Updated to use `EvaluationPaths` when provided
   - Maintains backward compatibility for standalone usage
   - Uses `metadata.json` instead of `args.json`
   - Creates proper subdirectory structure (workspace/, logs/, cache/, etc.)
   - Implements debug directory copying when `--keep-tmp` enabled
   - Updated `validate_output()` to work with new structure

## Migration Guide

### For Existing Evaluations

If you have existing evaluation directories, you can:

1. **Continue using them as-is** - old structure still works for reading results
2. **Migrate manually** - reorganize files to match new structure
3. **Use migration script** (future work):
   ```bash
   uv run scripts/migrate_eval_structure.py \
     --input cybergym_eval_6 \
     --output cybergym_eval_6_migrated
   ```

### For New Evaluations

Simply run with the updated code:

```bash
# Basic usage
uv run run_eval.py \
  --task-csv task_lists/tasks.csv \
  --times-per-problem 1 \
  --parallel-requests 1 \
  --output-dir cybergym_eval_7

# With debug mode
uv run run_eval.py \
  --task-csv task_lists/tasks.csv \
  --times-per-problem 1 \
  --parallel-requests 1 \
  --output-dir cybergym_eval_7 \
  --keep-tmp
```

## Benefits

### 1. **Improved Readability**
- Clear hierarchy: `runs/task/run_N/agent/` vs `logs/run_N/task-id/logs/`
- Obvious relationship between agent and judge results
- Self-documenting structure

### 2. **Better Organization**
- Related files grouped together
- Separate concerns (agent vs judge, permanent vs temporary)
- Consistent naming conventions

### 3. **Easier Debugging**
- Optional debug mode preserves tmp files
- Comprehensive metadata in each run
- Failed runs tracked separately

### 4. **Maintainability**
- Centralized path logic
- No hardcoded paths scattered throughout code
- Easy to extend for new evaluation types

### 5. **Cleaner Output**
- Temporary files don't clutter output directory
- Only essential results stored
- Summary files for quick overview

### 6. **Consistency**
- Database always in same location within evaluation
- Standard structure across all evaluations
- Predictable file locations

## Backward Compatibility

The changes maintain backward compatibility where possible:

1. **Database Migration**: Automatically migrates from `./server_poc/poc.db` or `./poc.db` to new location
2. **Standalone Usage**: `run.py` can still be used standalone without `eval_paths`
3. **Reading Old Results**: Old evaluation directories can still be read (though structure differs)

## Testing

To test the changes:

```bash
# Test with minimal task list
uv run run_eval.py \
  --task-csv task_lists/test.csv \
  --times-per-problem 1 \
  --parallel-requests 1 \
  --output-dir cybergym_eval_test

# Verify directory structure
tree cybergym_eval_test -L 4

# Check summary
cat cybergym_eval_test/summary.json

# Test with debug mode
uv run run_eval.py \
  --task-csv task_lists/test.csv \
  --times-per-problem 1 \
  --parallel-requests 1 \
  --output-dir cybergym_eval_test_debug \
  --keep-tmp

# Verify debug directory exists
ls cybergym_eval_test_debug/runs/*/run_0/agent/.debug/
```

## Future Enhancements

1. **Migration Script**: Automate conversion of old evaluation directories
2. **Result Analysis Tools**: Scripts to analyze summary.json across multiple evaluations
3. **Visualization**: Generate HTML reports from evaluation results
4. **Archiving**: Compress old evaluations while preserving structure
5. **Comparison Tools**: Compare results across different evaluation runs

## Questions / Issues

If you encounter any issues with the restructuring:

1. Check this document for expected behavior
2. Use `--keep-tmp` flag to inspect temporary files
3. Review `metadata.json` for run details
4. Check `failed_runs.json` for failure information
5. Report issues with full error messages and directory structure
