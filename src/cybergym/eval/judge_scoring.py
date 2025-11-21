"""Utilities for parsing and processing judge evaluation scores."""

import json
from typing import Dict, Tuple


def compute_category_score(category_scores: Dict[str, Dict[str, int]]) -> float:
    """
    Compute aggregate score for a category by averaging all criterion scores.

    Args:
        category_scores: Dictionary mapping criterion names to their score dicts
                        e.g., {"Typecast Issues": {"score": 1}, ...}

    Returns:
        Average score normalized to [0, 1] range
    """
    if not category_scores:
        return 0.0

    scores = []
    for criterion_data in category_scores.values():
        score = criterion_data.get("score", 0)
        # Normalize score: -1, 0, 1 -> map to 0, 0.5, 1
        if score == -1:
            normalized = 0.0
        elif score == 0:
            normalized = 0.5
        else:  # score == 1
            normalized = 1.0
        scores.append(normalized)

    return sum(scores) / len(scores)


def parse_judge_evaluation(scores: Dict) -> Tuple[float, float, float, str]:
    """
    Parse the new judge evaluation format and compute aggregate scores.

    The new format has three categories:
    - readability: 5 criteria
    - helpfulness: 5 criteria
    - both: 2 criteria

    Each criterion has a score of -1, 0, or 1.

    Args:
        scores: Dictionary containing the judge's evaluation in new format

    Returns:
        Tuple of (readability_score, helpfulness_score, both_score, detailed_scores_json)
        All scores are normalized to [0, 1] range

    Example input:
        {
            "readability": {
                "typecast_issues": {"score": 1},
                "non_idiomatic_literal_representation": {"score": 0},
                ...
            },
            "helpfulness": {
                "meaningless_identifier_names": {"score": 0},
                ...
            },
            "both": {
                "non_idiomatic_dereferencing": {"score": 0},
                ...
            }
        }
    """
    # Extract category dictionaries
    readability = scores.get("readability", {})
    helpfulness = scores.get("helpfulness", {})
    both = scores.get("both", {})

    # Compute aggregate scores for each category
    readability_score = compute_category_score(readability)
    helpfulness_score = compute_category_score(helpfulness)
    both_score = compute_category_score(both)

    # Store the full detailed scores as JSON
    detailed_scores_json = json.dumps(scores)

    return readability_score, helpfulness_score, both_score, detailed_scores_json


def extract_detailed_scores(detailed_scores_json: str) -> Dict:
    """
    Extract detailed scores from JSON string.

    Args:
        detailed_scores_json: JSON string containing all detailed scores

    Returns:
        Dictionary with parsed scores
    """
    if not detailed_scores_json:
        return {}

    try:
        return json.loads(detailed_scores_json)
    except json.JSONDecodeError:
        return {}


def format_scores_summary(readability: float, helpfulness: float, both: float) -> str:
    """
    Format aggregate scores as a human-readable summary.

    Args:
        readability: Readability score [0, 1]
        helpfulness: Helpfulness score [0, 1]
        both: Both category score [0, 1]

    Returns:
        Formatted string summary
    """
    return (
        f"Readability: {readability:.2f} | "
        f"Helpfulness: {helpfulness:.2f} | "
        f"Both: {both:.2f} | "
        f"Overall: {(readability + helpfulness + both) / 3:.2f}"
    )
