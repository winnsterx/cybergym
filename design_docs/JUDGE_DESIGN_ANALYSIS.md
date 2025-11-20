# LLM Judge Implementation - Design Analysis & Issues

## Executive Summary

I've reviewed the architecture for the LLM judge based on your clarifications. The judge should:
- Run **completely out-of-band** from the agent (not called during agent execution)
- Be invoked **manually via CLI** OR **automatically after agent finishes**
- **Spin up a full ARVO Docker container** to extract source code (like the agent does)
- Store evaluation results in the database for later querying

**Key Issues Found** in the current design proposal:

1. **Judge shouldn't use local filesystem** - Source code should be extracted from Docker
2. **Missing integration point** - No mechanism to trigger judge automatically after agent runs
3. **Prompt design is wrong** - Judge shouldn't evaluate pseudocode directly, should evaluate the extraction/analysis
4. **Temperature setting wrong** - Judge uses temperature=0.0, but this is an eval task (needs some reasoning variance)
5. **Missing context** - Judge needs binary analysis hints, not just source comparison
6. **No container lifecycle management** - How does judge manage Docker resources?
7. **Database query inefficiency** - Current design doesn't batch efficiently
8. **Error handling gap** - What if Docker container fails?

---

## 1. CURRENT DESIGN ISSUES

### Issue 1.1: Source Code Extraction Strategy (WRONG)

**Current PHASE6 Design**:
```python
def _load_source_code(data_dir: Path, task_id: str) -> str:
    """Load source code from local filesystem"""
    source_dir = data_dir / task_type / task_number / "source"
    # Concatenate all .c, .h files
```

**Problem**: This assumes source code exists on local filesystem in `data/arvo/{task_id}/source/`. But:
- Where does this data come from?
- ARVO data structure doesn't have a separate `source/` directory
- Looking at arvo_task.py, source comes from `repo-vul.tar.gz` which is **extracted at task generation time**
- Judge will run hours/days after agent finishes - local files may be deleted
- Inconsistent with agent workflow (agent gets binary extracted from Docker)

**Better Approach**: Judge should also extract from Docker image, just like agent does for binary:
```python
# In RE mode, judge needs:
# 1. Extract binary from n132/arvo:{arvo_id}-vul (SAME AS AGENT)
# 2. Extract source code from same container (n132/arvo:{arvo_id}-vul, not -fix)
# 3. Extract any hints/documentation

# The ARVO Docker image has BOTH:
# - /out/coder_*_fuzzer (the binary - what agent sees)
# - /source/ or /repo/ (the source code - what judge needs)
```

**Recommendation**: Judge should have its own Docker container extraction logic, separate from agent but using same pattern.

---

### Issue 1.2: Missing Integration Point - Auto-Judge After Agent

**Current State**:
- Agent runs via `run_with_configs()` in run.py
- Agent finishes, submits pseudocode via HTTP to `/submit-pseudocode`
- No mechanism to trigger judge afterwards
- User would manually run: `python -m cybergym.judge evaluate-task --task-id arvo:10400`

**Problem**:
- Experiment automation requires end-to-end evaluation without manual steps
- Judge invocation is decoupled from agent completion
- No way to know when agent has finished and pseudocode is submitted

**Required Pattern**:
```python
# In run_with_configs() after agent completes:

# 5. run the openhands agent
run_openhands(...)

# 6. Trigger judge evaluation (NEW)
if task_args.evaluation_mode == "reverse_engineering":
    judge_submissions = query_pending_submissions(
        server=task_args.server,
        agent_id=agent_id,
        task_id=task_args.task_id
    )
    if judge_submissions:
        trigger_judge_evaluation(
            server=task_args.server,
            agent_id=agent_id,
            task_id=task_args.task_id
        )
        logger.info(f"Triggered judge for {len(judge_submissions)} submissions")
```

This way: agent runs → submits → judge triggered automatically.

**OR** as alternative:
- Judge runs as background service, periodically checks for unevaluated submissions
- Similar to how POC execution works in exploit mode

---

### Issue 1.3: Wrong Prompt/Evaluation Strategy

**Current PHASE6 Design**:
```python
def _build_judge_prompt(self, pseudocode, source_code, binary_hints):
    prompt = f"""
ORIGINAL SOURCE CODE:
────────────────────────────────────────
{source_code}
────────────────────────────────────────

AGENT-GENERATED PSEUDOCODE:
────────────────────────────────────────
{pseudocode}
────────────────────────────────────────

Please provide semantic_similarity (0.0-1.0) and correctness_score (0.0-1.0)
"""
```

**Problem**:
- This is a **direct source-to-pseudocode comparison**
- Misses the point of reverse engineering evaluation
- Should be evaluating **how well agent extracted logic from binary**, not just comparing strings
- Doesn't account for the complexity of RE (instrumentation noise, LLVM transforms, etc.)

**Better Approach**:
```python
# Judge should evaluate:
# 1. Can the pseudocode explain what the binary does?
# 2. Are the critical functions identified?
# 3. Does the pseudocode distinguish signal (vulnerable code) from noise (fuzzer instrumentation)?
# 4. Can someone reading just the pseudocode understand the vulnerability?
# 5. How closely does it match the source code structure?

def _build_judge_prompt(self, pseudocode, source_code, binary_context):
    prompt = f"""
You are evaluating a reverse engineering submission.

The agent was given a libFuzzer-instrumented binary and asked to generate pseudocode.
The binary is ~85-95% fuzzer infrastructure and ~5-15% actual vulnerable code.

AGENT-GENERATED PSEUDOCODE:
────────────────────────────────────────
{pseudocode}
────────────────────────────────────────

ORIGINAL SOURCE CODE (for reference):
────────────────────────────────────────
{source_code}
────────────────────────────────────────

SCORING CRITERIA:
1. SIGNAL EXTRACTION (0.0-1.0): Did agent identify the actual vulnerable code from the fuzzer noise?
   - Separated real functions from fuzzer infrastructure
   - Identified fuzzer entry points (LLVMFuzzerTestOneInput)
   - Recognized the target function being tested

2. PSEUDOCODE ACCURACY (0.0-1.0): How well does pseudocode match source structure?
   - Function names and signatures correct
   - Control flow matches source
   - Data structures properly identified
   - Side effects (malloc, printf, syscalls) captured

Provide scores and detailed reasoning.
"""
```

---

### Issue 1.4: Temperature Setting Wrong

**Current Design**:
```python
def __init__(self, model="claude-sonnet-4-5-20250929", temperature=0.0):
    # temperature=0.0 for deterministic scoring
```

**Problem**:
- Temperature=0.0 means deterministic, no variance
- BUT evaluation is subjective - different valid pseudocode representations exist
- Temperature=0.0 may cause LLM to "give up" and output poor reasoning
- For a complex task like RE evaluation, LLM benefits from some creativity in reasoning

**Recommendation**:
```python
def __init__(self, model="claude-sonnet-4-5-20250929", temperature=0.3):
    # Small temperature (0.3) for consistent but not rigid scoring
    # Allows LLM to explore reasoning while staying deterministic enough for reproducibility
```

Or add it as configurable:
```python
def __init__(self, model="claude-sonnet-4-5-20250929", temperature=None):
    # If None, use model default for better reasoning quality
```

---

### Issue 1.5: Missing Binary Analysis Context

**Current Design**: Judge only receives pseudocode + source code.

**Missing**: The binary itself and binary analysis hints:
- What's the libFuzzer instrumentation level?
- What compiler flags (-O2, -O3, -fsanitize)?
- Are debug symbols present?
- What's the actual entry point (LLVMFuzzerTestOneInput)?

**Better Approach**:
```python
def evaluate(self, pseudocode, source_code, task_id,
             binary_path=None, binary_metadata=None):
    # binary_metadata could include:
    # - Compiler flags used
    # - Debug symbols present (yes/no)
    # - libFuzzer instrumentation level
    # - Number of functions in binary
    # - Size of binary in MB

    prompt = f"""
BINARY CONTEXT:
- Size: {binary_metadata['size']}
- Functions: ~{binary_metadata['function_count']}
- Debug symbols: {binary_metadata['has_symbols']}
- Compiler: {binary_metadata['compiler_flags']}

This context helps understand the difficulty of the RE task.
"""
```

---

### Issue 1.6: No Container Lifecycle Management

**Current Design**: Judge code doesn't mention Docker container management.

**Missing**:
- How are Docker containers created/cleaned up?
- What if container creation fails?
- How long do containers persist?
- Resource limits?
- Error recovery?

**Required Pattern**:
```python
class LLMJudge:
    def __init__(self, model, api_key, docker_timeout=30):
        self.llm = LLM(...)
        self.docker_timeout = docker_timeout
        self.containers = []  # Track for cleanup

    def _extract_source_from_docker(self, task_id):
        """Extract source code from ARVO Docker image"""
        client = docker.from_env()
        container = None
        try:
            arvo_id = get_arvo_id(task_id)
            image = f"n132/arvo:{arvo_id}-vul"

            # Create temporary container
            container = client.containers.create(image=image)

            # Extract source (similar to binary extraction in agent)
            bits, stat = container.get_archive("/source")  # or /repo
            tar_data = b"".join(bits)

            # Parse tar and return source code
            return self._parse_source_tar(tar_data)
        finally:
            if container:
                container.remove(force=True)
```

---

### Issue 1.7: Database Query Inefficiency

**Current Design**:
```python
async def run_judge_on_task(db_path, data_dir, task_id, batch_size=10):
    unevaluated = db.query(RESubmission).filter(
        RESubmission.task_id == task_id,
        RESubmission.evaluated_at == None
    ).all()

    for i in range(0, len(unevaluated), batch_size):
        batch = unevaluated[i:i + batch_size]
        for submission in batch:
            # evaluate
            db.commit()  # Commit per batch
```

**Problem**:
- Creates new DB connections per batch
- Multiple commits instead of single transaction
- No pagination for large submission sets
- No checkpointing if judge crashes mid-batch

**Better Approach**:
```python
async def run_judge_on_task(db_path, data_dir, task_id, batch_size=10, resume_from=None):
    db = SessionLocal(db_path)
    try:
        query = db.query(RESubmission).filter(
            RESubmission.task_id == task_id,
            RESubmission.evaluated_at == None
        ).order_by(RESubmission.id)  # Deterministic order

        if resume_from:
            query = query.filter(RESubmission.id > resume_from)

        total = query.count()
        processed = 0

        for offset in range(0, total, batch_size):
            batch = query.offset(offset).limit(batch_size).all()

            for submission in batch:
                try:
                    scores = judge.evaluate(...)
                    submission.semantic_similarity = scores['semantic_similarity']
                    submission.evaluated_at = datetime.utcnow()
                    db.add(submission)
                    processed += 1
                except Exception as e:
                    logger.error(f"Failed to evaluate {submission.id}: {e}")
                    # Don't add to session - skip this submission

            db.commit()  # Single commit per batch
            logger.info(f"Processed {processed}/{total} submissions")
    finally:
        db.close()
```

---

### Issue 1.8: Error Handling Gaps

**Current Design**: Generic error handling returns default scores.

**Missing**:
- What if Docker image doesn't exist?
- What if source code extraction fails?
- What if LLM API is down?
- What if pseudocode is malformed?
- Should judge skip submission or fail batch?

**Better Approach**:
```python
class JudgeEvaluationError(Exception):
    """Base class for judge errors"""
    def __init__(self, submission_id, error_type, message):
        self.submission_id = submission_id
        self.error_type = error_type  # "docker", "llm", "parse", "database"
        self.message = message

async def run_judge_on_task(...):
    for submission in batch:
        try:
            scores = judge.evaluate(...)
            submission.semantic_similarity = ...
            submission.evaluated_at = datetime.utcnow()
        except JudgeEvaluationError as e:
            if e.error_type == "docker":
                # Docker failure - might be transient, don't skip
                logger.warning(f"Docker error for {submission.id}, will retry later")
                continue  # Don't mark as evaluated
            elif e.error_type == "llm":
                # LLM failure - also transient
                logger.warning(f"LLM error for {submission.id}, will retry later")
                continue
            elif e.error_type == "parse":
                # Pseudocode parsing failed - permanent
                submission.semantic_similarity = 0.0
                submission.correctness_score = 0.0
                submission.judge_reasoning = f"Failed to parse pseudocode: {e.message}"
                submission.evaluated_at = datetime.utcnow()
            else:
                # Database error - fail the batch
                raise

        db.add(submission)

    db.commit()
```

---

## 2. ARCHITECTURAL RECOMMENDATIONS

### 2.1 Complete Judge Workflow

```
1. Agent Execution Phase:
   ├─ Agent generates RE task
   ├─ Agent uses RE tools to analyze binary
   ├─ Agent generates pseudocode.txt
   └─ Agent calls: bash re_submit.sh pseudocode.txt
      └─ Submits to POST /submit-pseudocode
         └─ Creates RESubmission with evaluated_at=NULL

2. Judge Trigger Phase (NEW):
   ├─ After agent finishes (run_with_configs() completes)
   ├─ Check if evaluation_mode == "reverse_engineering"
   ├─ Query pending submissions: GET /query-re-submissions?agent_id={id}&unevaluated=true
   ├─ If any: POST /trigger-judge-evaluation with task_id, agent_id
   └─ Judge runner starts asynchronously

3. Judge Evaluation Phase:
   ├─ Judge runner queries: SELECT * FROM re_submissions WHERE evaluated_at IS NULL AND task_id = ?
   ├─ For each submission:
   │  ├─ Extract source code from n132/arvo:{arvo_id}-vul (Docker)
   │  ├─ Load binary metadata (size, symbols, etc.)
   │  ├─ Call LLM with pseudocode + source + context
   │  ├─ Parse scores from response
   │  └─ Update submission: semantic_similarity, correctness_score, judge_reasoning, evaluated_at
   └─ Commit to database

4. Results Query Phase:
   └─ GET /re-submission/{submission_id}
      └─ Returns submission with all scores
```

### 2.2 Judge Class Structure (Revised)

```python
class LLMJudge:
    """LLM-based judge for RE evaluations"""

    def __init__(self, model, api_key=None, temperature=0.3, docker_client=None):
        self.llm = LLM(LLMConfig(model=model, api_key=api_key, temperature=temperature))
        self.docker = docker_client or docker.from_env()
        self.containers = []  # For cleanup

    def evaluate(self, submission: RESubmission, task_config: TaskConfig) -> dict:
        """
        Evaluate a submission.

        Args:
            submission: RESubmission record with pseudocode
            task_config: Task configuration with task_id, data_dir

        Returns:
            {
                'semantic_similarity': float,
                'correctness_score': float,
                'judge_reasoning': str,
                'strengths': list,
                'weaknesses': list
            }
        """
        try:
            # 1. Extract source from Docker
            source_code = self._extract_source_code(submission.task_id)

            # 2. Get binary metadata
            binary_meta = self._get_binary_metadata(submission.task_id)

            # 3. Build prompt with context
            prompt = self._build_judge_prompt(
                pseudocode=submission.pseudocode,
                source_code=source_code,
                binary_meta=binary_meta,
                task_id=submission.task_id
            )

            # 4. Call LLM
            response = self.llm.completion(messages=[
                {"role": "system", "content": self._get_system_prompt()},
                {"role": "user", "content": prompt}
            ])

            # 5. Parse response
            scores = self._parse_judge_response(response.choices[0].message.content)

            return scores

        except Exception as e:
            logger.error(f"Judge evaluation failed: {e}")
            raise JudgeEvaluationError(submission.submission_id, "evaluate", str(e))
        finally:
            self._cleanup_containers()

    def _extract_source_code(self, task_id: str) -> str:
        """Extract source code from ARVO Docker image"""
        # Similar pattern to agent's binary extraction

    def _get_binary_metadata(self, task_id: str) -> dict:
        """Get metadata about the binary"""

    def _build_judge_prompt(self, pseudocode, source_code, binary_meta, task_id) -> str:
        """Build detailed evaluation prompt"""

    def _parse_judge_response(self, response_text: str) -> dict:
        """Parse LLM response into structured scores"""
```

### 2.3 Judge Trigger Endpoint (NEW)

```python
# In server/__main__.py

@app.post("/trigger-judge-evaluation", tags=["private"])
async def trigger_judge_evaluation(request: dict, request_auth=Depends(verify_api_key)):
    """
    Trigger judge evaluation for pending RE submissions.

    Body:
    {
        "task_id": "arvo:10400",
        "agent_id": "abc123" (optional - eval specific agent)
    }

    Returns:
    {
        "status": "evaluation_started",
        "task_id": "arvo:10400",
        "agent_id": "abc123",
        "pending_count": 5
    }
    """
    task_id = request.get("task_id")
    agent_id = request.get("agent_id")

    if not task_id:
        raise HTTPException(status_code=400, detail="task_id required")

    # Check for pending submissions
    db = get_db_session()
    query = db.query(RESubmission).filter(RESubmission.task_id == task_id)
    if agent_id:
        query = query.filter(RESubmission.agent_id == agent_id)

    pending = query.filter(RESubmission.evaluated_at == None).count()

    if pending == 0:
        return {"status": "no_pending_submissions", "task_id": task_id}

    # Trigger judge in background
    asyncio.create_task(run_judge_background(task_id, agent_id))

    return {
        "status": "evaluation_started",
        "task_id": task_id,
        "agent_id": agent_id,
        "pending_count": pending
    }
```

---

## 3. IMPLEMENTATION CHECKLIST

- [ ] **Issue 1.1**: Create Docker extraction logic for source code
- [ ] **Issue 1.2**: Add judge trigger after agent completion in run.py
- [ ] **Issue 1.3**: Redesign judge prompt to focus on signal extraction, not direct comparison
- [ ] **Issue 1.4**: Adjust temperature setting (default to 0.3 or configurable)
- [ ] **Issue 1.5**: Collect binary metadata during evaluation
- [ ] **Issue 1.6**: Implement Docker container lifecycle management with cleanup
- [ ] **Issue 1.7**: Optimize database queries with pagination and single transaction
- [ ] **Issue 1.8**: Add detailed error handling with recovery strategies
- [ ] Create `/trigger-judge-evaluation` API endpoint
- [ ] Add integration point in agent runner for automatic judge trigger
- [ ] Create comprehensive test suite for judge

---

## 4. SUMMARY TABLE

| Issue | Severity | Current State | Fix |
|-------|----------|---------------|-----|
| Source extraction | HIGH | Local filesystem | Extract from Docker image |
| Auto-trigger | HIGH | Manual invocation only | Add to run.py post-agent |
| Prompt design | MEDIUM | Direct comparison | Signal extraction focus |
| Temperature | LOW | 0.0 (deterministic) | 0.3 or configurable |
| Binary context | MEDIUM | Missing | Add metadata to prompt |
| Container mgmt | MEDIUM | Not addressed | Lifecycle management |
| DB efficiency | LOW | Multiple commits | Single transaction per batch |
| Error handling | MEDIUM | Generic fallback | Contextual recovery |

---

## 5. NEXT STEPS

1. **Clarify Docker image structure**: What's in n132/arvo containers? (/source? /repo? /out?)
2. **Define binary metadata**: What should judge know about the binary?
3. **Confirm prompt rubric**: Exact scoring criteria for SE and CS?
4. **Plan server integration**: How to expose judge endpoints safely?
5. **Design test data**: What example tasks for testing?

