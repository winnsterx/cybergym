# Phase 3 Quick Reference

## What Was Built
Two new API endpoints for RE pseudocode submission and querying.

## Endpoints

### POST /submit-pseudocode (PUBLIC)
Submit agent's pseudocode for evaluation

**Request:**
```json
{
  "task_id": "arvo:10400",
  "agent_id": "agent_xyz",
  "checksum": "sha256...",
  "pseudocode": "int main() { ... }"
}
```

**Response:**
```json
{
  "submission_id": "sub_abc123",
  "task_id": "arvo:10400",
  "agent_id": "agent_xyz",
  "status": "received_for_evaluation"
}
```

**Key Features:**
- Checksum verification (validates task_id/agent_id)
- SHA256 hash-based deduplication
- Returns existing submission_id for duplicates
- No container execution needed

---

### POST /query-re-submissions (PRIVATE - requires API key)
Query submitted pseudocode for evaluation

**Request:**
```json
{
  "agent_id": "agent_xyz",
  "task_id": "arvo:10400"
}
```

**Response:**
```json
[
  {
    "agent_id": "agent_xyz",
    "task_id": "arvo:10400",
    "submission_id": "sub_abc123",
    "pseudocode_hash": "sha256...",
    "semantic_similarity": 0.85,
    "correctness_score": 0.92,
    "judge_reasoning": "...",
    "strengths": "[...]",
    "weaknesses": "[...]",
    "created_at": "2025-11-17T...",
    "evaluated_at": null
  }
]
```

---

## Files Changed

| File | Changes | Lines |
|------|---------|-------|
| `server/types.py` | Added `RESubmissionPayload`, `RESubmissionQuery` | +10 |
| `server/server_utils.py` | Added `submit_pseudocode()` function | +55 |
| `server/__main__.py` | Added endpoints + imports | +60 |

## How It Works

```
Agent                    Server                  Database
  |                        |                        |
  |--submit_pseudocode---->|                        |
  |                        |--verify checksum      |
  |                        |                        |
  |                        |--check hash---------->|
  |                        |<--exists? (dup)--------|
  |                        |                        |
  |                        |--create/get record--->|
  |<--submission_id--------|<--id+------|
  |
  |  (Later, after judge evaluation)
  |
  |--query_re_subs------->|
  |                        |--get records-------->|
  |                        |<--with scores--------|
  |<--results (with eval)-|
```

## Deduplication Logic

```
pseudocode_hash = SHA256(pseudocode_text)

Unique constraint: (agent_id, task_id, pseudocode_hash)

Result:
- Same agent + same task + same pseudocode = CACHED
- Different agent | different task = NEW record
- Different pseudocode = NEW record
```

## Integration with Phase 1
- Uses `RESubmission` model
- Uses `get_or_create_re_submission()` helper
- Uses `query_re_submissions()` helper
- Uses `update_re_submission_scores()` for judge results

## Testing
All 8 tests passing:
- New submissions
- Duplicate detection
- Invalid checksum rejection
- Multi-agent support
- Multi-task support
- Hash deduplication
- Query filtering
- Storage/retrieval

## Ready For
- **Phase 4:** Agent runner integration
- **Phase 5:** Agent prompts (instruction to use this endpoint)
- **Phase 6:** Judge evaluation (will score submissions)
- **Phase 7:** Judge API (will query/trigger evaluation)
