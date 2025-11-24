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
        - scores_dict: Dict mapping category names to normalized [0,1] scores
        - detailed_scores_json: JSON string of full evaluation
    """
    schemas = _load_schemas()

    if schema_name not in schemas:
        raise KeyError(f"Unknown schema: {schema_name}. Available: {list(schemas.keys())}")

    schema = schemas[schema_name]
    categories = schema["categories"]
    scores = {}

    for category_name, score_range in categories.items():
        min_score, max_score = score_range

        # Extract category from evaluation JSON
        category_data = evaluation_json.get(category_name, {})

        if not category_data:
            scores[category_name] = 0.0
            continue

        # Extract all criterion scores from this category
        criterion_scores = []
        for criterion_value in category_data.values():
            if isinstance(criterion_value, dict) and "score" in criterion_value:
                raw_score = criterion_value["score"]
                # Normalize to [0, 1]
                normalized = (raw_score - min_score) / (max_score - min_score) if max_score > min_score else 0.5
                criterion_scores.append(normalized)
            elif isinstance(criterion_value, (int, float)):
                # Direct score (already normalized for simple schema)
                normalized = (criterion_value - min_score) / (max_score - min_score) if max_score > min_score else criterion_value
                criterion_scores.append(normalized)

        # Average all criteria in this category
        scores[category_name] = sum(criterion_scores) / len(criterion_scores) if criterion_scores else 0.0

    return scores, json.dumps(evaluation_json)


def list_schemas() -> list[str]:
    """List all available grading schemas."""
    schemas = _load_schemas()
    return list(schemas.keys())
