# Modular Grading Schema System - Implementation Summary

## Overview

Implemented a flexible, modular grading schema system that allows easy experimentation with different judge output formats without requiring changes throughout the codebase.

## Key Changes

### 1. New Grading Schema Configuration (`src/cybergym/eval/grading_schemas.json`)

Defines all supported grading schemas in one centralized file:

```json
{
  "five-point": {
    "description": "5-point scale with 4 categories and 10 sub-criteria",
    "categories": {
      "behavioral_correctness": [-2, 2],
      "data_model_clarity": [-2, 2],
      "naming_quality": [-2, 2],
      "readability": [-2, 2]
    }
  },
  "simple": {
    "description": "Simple 2-category direct scoring",
    "categories": {
      "readability": [0, 1],
      "helpfulness": [0, 1]
    }
  }
}
```

### 2. Generic Parser (`src/cybergym/eval/judge_parser.py`)

- Reads schema configuration and parses any JSON shape
- Returns flexible dict of category scores
- All scores normalized to [0, 1] range
- Easy to add new schemas - just update the JSON file

### 3. Database Schema Updates (`src/cybergym/server/pocdb.py`)

**RESubmission table:**
- **Removed:** `readability_score`, `helpfulness_score`, `both_score` (fixed schema)
- **Added:**
  - `grading_schema`: String (e.g., "five-point", "simple")
  - `category_scores`: JSON dict mapping category names to normalized scores
  - `detailed_scores`: Full evaluation JSON (unchanged)

### 4. CLI Argument (`run_eval.py`)

New argument to specify grading schema:
```bash
python run_eval.py \
  --grading-schema five-point \
  ...other args...
```

### 5. Summary Output

`summary.json` now includes:
- `grading_schema`: Which schema was used
- `overall_metrics`: Dynamic dict with all categories from that schema
- `tasks[task_id].metrics`: Dynamic dict with all categories

Example for five-point:
```json
{
  "grading_schema": "five-point",
  "overall_metrics": {
    "behavioral_correctness": {"mean": 0.75, "median": 0.80, ...},
    "data_model_clarity": {"mean": 0.68, "median": 0.70, ...},
    "naming_quality": {"mean": 0.82, "median": 0.85, ...},
    "readability": {"mean": 0.90, "median": 0.95, ...}
  }
}
```

## How to Add a New Schema

1. **Add to `grading_schemas.json`:**
```json
{
  "my-new-schema": {
    "description": "My custom grading format",
    "categories": {
      "category1": [min_score, max_score],
      "category2": [min_score, max_score]
    }
  }
}
```

2. **That's it!** The parser automatically:
   - Extracts category data from judge JSON
   - Normalizes scores to [0, 1]
   - Stores in database
   - Generates statistics

## Usage Examples

### Run evaluation with five-point schema (default):
```bash
python run_eval.py \
  --task-csv tasks.csv \
  --times-per-problem 3 \
  --parallel-requests 5 \
  --output-dir results \
  --grading-schema five-point
```

### Run evaluation with simple schema:
```bash
python run_eval.py \
  --task-csv tasks.csv \
  --times-per-problem 3 \
  --parallel-requests 5 \
  --output-dir results \
  --grading-schema simple
```

## Judge Output Format

### Five-Point Schema
Expects this structure:
```json
{
  "behavioral_correctness": {
    "control_flow_and_conditions": {"score": -2 to 2, "reasoning": "..."},
    "data_flow_and_reachability": {"score": -2 to 2, "reasoning": "..."}
  },
  "data_model_clarity": {
    "type_correctness": {"score": -2 to 2, "reasoning": "..."},
    ...
  },
  "naming_quality": {...},
  "readability": {...},
  "summary": {...}
}
```

### Simple Schema
Expects this structure:
```json
{
  "readability": 0.75,
  "helpfulness": 0.82,
  "total": 0.78,
  "reasoning": "..."
}
```

## Migration Notes

Since you mentioned no data exists yet:
- No migration needed
- Database will auto-create new schema on first run
- Old code referencing `readability_score`, `helpfulness_score`, `both_score` will need to be updated to use `category_scores` JSON field

## Web Viewer Updates

The web database viewer (`web_db_viewer.py`) has been updated to support flexible schemas:

### List View
- Shows grading schema name for each submission
- Displays abbreviated category scores (e.g., "BC: 0.75 DMC: 0.68")

### Detail View
- Shows full grading schema name
- Displays all category scores in a grid
- Dynamically renders detailed breakdown for any schema
- Shows criterion-level scores with reasoning
- Adapts to any number of categories/criteria

Run the viewer:
```bash
python web_db_viewer.py
```
Then open http://localhost:8765 in your browser.

## Benefits

1. **Zero code changes** to add new schemas - just edit JSON
2. **Flexible output** - each schema can have different categories
3. **Automatic normalization** - scores always in [0, 1] range
4. **Dynamic statistics** - summary adapts to schema
5. **Easy experimentation** - switch schemas with one flag
