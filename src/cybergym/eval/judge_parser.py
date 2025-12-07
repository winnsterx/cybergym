"""Generic judge evaluation parser based on grading schemas."""

import json
from pathlib import Path
from typing import Dict, Tuple

_SCHEMA_FILE = Path(__file__).parent / "grading_schemas.json"
_SCHEMAS = None


def _load_schemas() -> Dict:
    """Load grading schemas from JSON file."""
    global _SCHEMAS
    if _SCHEMAS is None:
        with open(_SCHEMA_FILE) as f:
            _SCHEMAS = json.load(f)
    return _SCHEMAS


def parse_judge_evaluation(evaluation_json: Dict, schema_name: str = "five-point") -> Tuple[Dict[str, float], str]:
    """
    Parse judge evaluation using specified grading schema.

    Args:
        evaluation_json: Raw evaluation JSON from judge
        schema_name: Name of grading schema to use

    Returns:
        Tuple of (scores_dict, detailed_scores_json)
        - scores_dict: Dict mapping category names to raw scores (not normalized)
        - detailed_scores_json: JSON string of full evaluation
    """
    schemas = _load_schemas()

    if schema_name not in schemas:
        raise KeyError(f"Unknown schema: {schema_name}. Available: {list(schemas.keys())}")

    schema = schemas[schema_name]
    categories = schema["categories"]
    scores = {}

    for category_name, score_range in categories.items():
        # Extract category from evaluation JSON
        category_data = evaluation_json.get(category_name, {})

        if not category_data:
            scores[category_name] = 0.0
            continue

        # New flat format: category has direct "score" field
        if isinstance(category_data, dict) and "score" in category_data:
            scores[category_name] = category_data["score"]
        # Legacy nested format: category has sub-criteria with scores
        elif isinstance(category_data, dict):
            criterion_scores = []
            for criterion_value in category_data.values():
                if isinstance(criterion_value, dict) and "score" in criterion_value:
                    criterion_scores.append(criterion_value["score"])
                elif isinstance(criterion_value, (int, float)):
                    criterion_scores.append(criterion_value)
            # Average all criteria in this category
            scores[category_name] = sum(criterion_scores) / len(criterion_scores) if criterion_scores else 0.0
        elif isinstance(category_data, (int, float)):
            # Direct numeric score at category level
            scores[category_name] = category_data
        else:
            scores[category_name] = 0.0

    return scores, json.dumps(evaluation_json)


def list_schemas() -> list[str]:
    """List all available grading schemas."""
    schemas = _load_schemas()
    return list(schemas.keys())
