# Flare-On CTF Integration for CyberGym

This document describes how to set up and run Flare-On CTF challenges in CyberGym.

## Overview

The Flare-On integration allows CyberGym agents to solve Flare-On reverse engineering CTF challenges. Agents receive challenge files, analyze them using various reverse engineering tools, and submit flags for immediate verification.

## Setup

### 1. Clone Flare-On Challenges Repository

```bash
cd /mnt/jailbreak-defense/exp/winniex
git clone https://github.com/Flare-On/Flare-On-Challenges.git
```

### 2. Run Setup Script

```bash
cd cybergym
uv run python setup_flareon_challenges.py
```

This script will:
- Copy challenge archives from `../Flare-On-Challenges/Challenges/2024/` to `cybergym_data/data/flare-on/`
- Create `answers.csv` with placeholder flags
- Generate `task_lists/flare-on-2024.csv` with all challenges

### 3. Update Flags in answers.csv

The setup script creates placeholder flags. You need to update them with actual flags:

```bash
# Edit cybergym_data/data/flare-on/answers.csv
nano cybergym_data/data/flare-on/answers.csv
```

Format:
```csv
task,flag
flare-on:2024-01,actual_flag_here
flare-on:2024-02,another_flag_here
...
```

Flags can be found in the write-ups at `../Flare-On-Challenges/Write-ups/2024/`.

## Architecture

### Task Structure

```
cybergym_data/data/flare-on/
├── answers.csv                    # Maps task IDs to correct flags
├── 2024-01/
│   └── challenge.7z              # Encrypted with password "flare"
├── 2024-02/
│   └── challenge.7z
...
```

### Workflow

1. **Task Generation** (`flare_on_task.py`)
   - Extracts challenge archive (with password "flare")
   - Creates README.md with instructions
   - Generates submit_flag.sh script
   - Sets up workspace directory

2. **Agent Interaction**
   - Agent receives extracted challenge files in workspace
   - Uses reverse engineering tools (gdb, objdump, etc.)
   - Submits flag via: `bash submit_flag.sh FLAG_HERE`

3. **Flag Verification** (server endpoint `/submit-flag`)
   - Verifies agent checksum
   - Compares submitted flag against answers.csv
   - Stores result in FlareOnSubmission table
   - Returns immediate feedback (correct/incorrect)

4. **Evaluation** (`run_eval.py`)
   - Queries database for correct submissions
   - Generates statistics and summary.json
   - No judge agent needed (unlike RE mode)

### Database Schema

```sql
CREATE TABLE flareon_submissions (
    id INTEGER PRIMARY KEY,
    agent_id TEXT,
    task_id TEXT,
    submission_id TEXT UNIQUE,
    submitted_flag TEXT,
    flag_hash TEXT,
    correct INTEGER,  -- 1 = correct, 0 = incorrect
    created_at TIMESTAMP
);
```

## Running Evaluations

### Single Challenge Test

```bash
python run_eval.py \
  --task-csv task_lists/flare-on-sample.csv \
  --times-per-problem 1 \
  --parallel-requests 1 \
  --output-dir cybergym_eval_flareon_test \
  --evaluation-mode flare-on \
  --data-dir ./cybergym_data/data
```

### Full 2024 Challenge Set

```bash
python run_eval.py \
  --task-csv task_lists/flare-on-2024.csv \
  --times-per-problem 3 \
  --parallel-requests 5 \
  --output-dir cybergym_eval_flareon_2024 \
  --evaluation-mode flare-on \
  --model claude-sonnet-4-5-20250929 \
  --max-iter 100 \
  --timeout 3600
```

## File Structure

### Core Implementation Files

```
src/cybergym/
├── task/
│   ├── types.py                    # Added TaskType.FLARE_ON
│   ├── flare_on_task.py           # Challenge extraction & workspace setup
│   ├── gen_task.py                # Registered flare-on generator
│   ├── FLAREON.template           # README template for challenges
│   └── flare_on_submit.template   # Submission script template
├── server/
│   ├── __main__.py                # Added /submit-flag endpoint
│   ├── pocdb.py                   # Added FlareOnSubmission model
│   ├── server_utils.py            # Added submit_flag() function
│   └── types.py                   # Added FlareOnSubmissionPayload
└── eval/
    └── ...                        # Path management (reused from RE mode)
```

### Support Files

```
cybergym/
├── run_eval.py                    # Updated for flare-on mode
├── setup_flareon_challenges.py   # Setup script
├── test_flareon_integration.py   # Integration tests
└── task_lists/
    ├── flare-on-sample.csv       # Sample (2 challenges)
    └── flare-on-2024.csv         # Full set (9 challenges)
```

## Testing

### Unit Tests

```bash
# Run integration tests
uv run python test_flareon_integration.py
```

Tests verify:
- Database schema (FlareOnSubmission table)
- Task generation (archive extraction, file creation)
- Submit script generation and permissions

### Manual Testing

```bash
# 1. Generate a single task manually
cd examples/agents/openhands
uv run python -m cybergym.task.gen_task \
  --task-id flare-on:2024-01 \
  --data-dir ../../../cybergym_data/data \
  --server http://localhost:8666 \
  --out-dir /tmp/test_flare \
  --evaluation_mode flare-on

# 2. Check generated files
ls -la /tmp/test_flare/
cat /tmp/test_flare/README.md

# 3. Test flag submission (requires server running)
cd /tmp/test_flare
bash submit_flag.sh "test_flag"
```

## API Endpoints

### POST /submit-flag

Submit a flag for verification.

**Request:**
```json
{
  "task_id": "flare-on:2024-01",
  "agent_id": "abc123...",
  "checksum": "def456...",
  "flag": "flag{example}"
}
```

**Response (Correct):**
```json
{
  "submission_id": "flareon_abc123",
  "task_id": "flare-on:2024-01",
  "agent_id": "abc123...",
  "correct": true,
  "message": "Correct flag!",
  "created": true
}
```

**Response (Incorrect):**
```json
{
  "submission_id": "flareon_def456",
  "task_id": "flare-on:2024-01",
  "agent_id": "abc123...",
  "correct": false,
  "message": "Incorrect flag",
  "created": true
}
```

### POST /query-flareon-submissions

Query submissions by agent, task, or correctness.

**Request:**
```json
{
  "agent_id": "abc123...",
  "task_id": "flare-on:2024-01",  // optional
  "correct": 1  // optional: 1 = correct, 0 = incorrect
}
```

**Response:**
```json
[
  {
    "agent_id": "abc123...",
    "task_id": "flare-on:2024-01",
    "submission_id": "flareon_abc123",
    "flag_hash": "sha256...",
    "correct": 1,
    "created_at": "2025-11-20T..."
  }
]
```

## Comparison with Other Modes

| Feature | Exploit Mode | RE Mode | Flare-On Mode |
|---------|--------------|---------|---------------|
| Task Type | arvo, oss-fuzz | arvo | flare-on |
| Input | Source code | Binary + hints | CTF challenge archive |
| Output | PoC exploit | Pseudocode | Flag string |
| Verification | Docker execution | Judge agent | Exact string match |
| Database | PoCRecord | RESubmission | FlareOnSubmission |
| Endpoint | /submit-vul | /submit-pseudocode | /submit-flag |
| Judge Needed | No | Yes | No |

## Troubleshooting

### Issue: "Cannot open encrypted archive"

**Solution:** Ensure 7z is installed with password support:
```bash
sudo apt-get install p7zip-full
# or
brew install p7zip  # macOS
```

### Issue: "No answer found for task"

**Solution:** Update `cybergym_data/data/flare-on/answers.csv` with the correct flag for that task.

### Issue: "challenge/ directory empty"

**Solution:** Check that challenge.7z exists and can be extracted:
```bash
cd cybergym_data/data/flare-on/2024-01
7z l challenge.7z -pflare
7z x challenge.7z -pflare
```

### Issue: Evaluation shows 0% success rate

**Possible causes:**
1. Flags in answers.csv don't match actual flags
2. Agent submission format incorrect
3. Server not accessible from agent workspace

**Debug:**
```bash
# Check database for submissions
sqlite3 cybergym_eval_flareon/poc.db "SELECT * FROM flareon_submissions;"

# Check agent logs
cat cybergym_eval_flareon/runs/flare-on:2024-01/run_0/agent/logs/*.log
```

## Performance Tips

1. **Parallel execution**: Use `--parallel-requests 5` for faster evaluation
2. **Timeout settings**: CTF challenges may need longer timeouts (`--timeout 3600`)
3. **Iteration limits**: Complex challenges need more iterations (`--max-iter 150`)
4. **Model selection**: Claude Sonnet 4.5 performs best on reverse engineering tasks

## Future Enhancements

- [ ] Support for multi-part challenges
- [ ] Partial credit for partial flags
- [ ] Hint system integration
- [ ] Time-based scoring
- [ ] Support for other CTF competitions (picoCTF, etc.)

## References

- Flare-On Challenges: https://github.com/Flare-On/Flare-On-Challenges
- CyberGym Documentation: [EXTENSION.md](EXTENSION.md)
- OpenHands Integration: [examples/agents/openhands/](examples/agents/openhands/)
