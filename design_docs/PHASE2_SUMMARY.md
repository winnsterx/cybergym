# Phase 2 Implementation Summary

## Quick Reference

### What Phase 2 Does
Implements conditional file filtering in task generation to support Reverse Engineering (RE) evaluation mode, enabling the same task data (ARVO/OSS-Fuzz binaries) to be used in two different modes:
- **Exploit Mode**: Full file set (source, patch, description)
- **RE Mode**: Filtered file set (binary + optional hints only)

---

## Key Architecture Decisions

### 1. Reuse Existing Data
- **NO new task IDs**: Same `arvo:10400` used for both modes
- **NO data duplication**: Binary file used for both exploit and RE evaluation
- **Conditional file filtering**: evaluation_mode parameter controls what gets copied

### 2. Minimal Code Changes
- Only 6 files modified
- ~120 lines of actual code changes
- All changes are isolated and additive (no refactoring of existing logic)
- 100% backward compatible (evaluation_mode defaults to "exploit")

### 3. Clean Separation of Concerns
- Task generation: File filtering only
- Submission: Different endpoint per mode (Phase 3)
- Evaluation: Different approach per mode (Phase 6+)
- Agent execution: Prompt selection per mode (Phase 4)

---

## Files Modified: Line-by-Line Changes

### 1. `types.py` - 1 Line Addition

```python
# Add to TaskConfig dataclass (line 47)
evaluation_mode: str = "exploit"  # "exploit" or "reverse_engineering"
```

### 2. `arvo_task.py` - ~100 Lines

**New Constants** (after line 39):
- `RE_DIFFICULTY_FILES` - Maps difficulty to optional files in RE mode
- `RE_ARVO_FILES` - File descriptions for RE mode README

**Modified Function** - `prepare_arvo_files()`:
- Add `evaluation_mode: str = "exploit"` parameter
- Replace file selection logic with conditional:
  ```
  if evaluation_mode == "reverse_engineering":
      copy: binary + optional hints (level-dependent)
  else:
      copy: standard difficulty files (unchanged)
  ```
- Add template selection:
  ```
  if evaluation_mode == "reverse_engineering":
      use RE.template and re_submit.template
  else:
      use standard templates (unchanged)
  ```

**Updated Function** - `generate_arvo_task()`:
- Pass `evaluation_mode=config.evaluation_mode` to prepare_arvo_files()
- Populate `evaluation_mode` and `task_type` in returned Task

### 3. `oss_fuzz_task.py` - ~10 Lines

**Both Functions** - `generate_oss_fuzz_task()` and `generate_oss_fuzz_latest_task()`:
- Pass `evaluation_mode=config.evaluation_mode` to prepare_arvo_files()
- Populate `evaluation_mode` and `task_type` in returned Task
- Fix docstring (was incorrectly labeled as "ARVO task")

### 4. `gen_task.py` - ~8 Lines

**In `init_parser()`**:
```python
parser.add_argument(
    "--evaluation-mode",
    type=str,
    default="exploit",
    choices=["exploit", "reverse_engineering"],
    help="Evaluation mode: 'exploit' (default) or 'reverse_engineering'",
)
```

**In `main()`**:
```python
config = TaskConfig(
    ...
    evaluation_mode=args.evaluation_mode,  # NEW
)
```

### 5. `RE.template` - NEW FILE

Markdown template for RE task README with:
- Clear objective of RE task
- List of available RE tools
- Guidance on good pseudocode
- Submission instructions

### 6. `re_submit.template` - NEW FILE

Bash script template for RE submission that:
- POSTs to `/submit-pseudocode` (not `/submit-vul`)
- Includes task_id, agent_id, checksum metadata
- Validates file exists before upload

---

## File Generation Examples

### Exploit Mode (Default - Unchanged)
```
Command:
  gen_task --task-id arvo:10400 --out-dir /tmp/exploit --difficulty level1

Generated Files:
  /tmp/exploit/
  ├── README.md (instructions for PoC generation)
  ├── submit.sh (POSTs to /submit-vul endpoint)
  ├── repo-vul.tar.gz (vulnerable source)
  └── description.txt (vulnerability description)
```

### RE Mode (New)
```
Command:
  gen_task --task-id arvo:10400 --out-dir /tmp/re --difficulty level1 --evaluation-mode reverse_engineering

Generated Files:
  /tmp/re/
  ├── README.md (instructions for RE, lists available tools)
  ├── re_submit.sh (POSTs to /submit-pseudocode endpoint)
  ├── binary (the executable to reverse engineer)
  ├── hints.txt (high-level hints, because level1)
  └── output_example.txt (not included, because level < 2)
```

### RE Mode with level2
```
Generated Files:
  /tmp/re/
  ├── README.md
  ├── re_submit.sh
  ├── binary
  ├── hints.txt (included, level >= 1)
  └── output_example.txt (included, level >= 2)
```

---

## File Filtering Logic (Core of Phase 2)

### RE Mode File Selection

```
Always include:
  - binary (or binaries/*.vul)

Level-based optional files:
  level0: (none)
  level1: hints.txt
  level2: hints.txt, output_example.txt
  level3: hints.txt, output_example.txt

Never include (regardless of level):
  - repo-vul.tar.gz (source code exposure)
  - repo-fix.tar.gz (patch leakage)
  - description.txt (exploit hints)
  - patch.diff (vulnerability details)
  - error.txt (PoC guidance)
```

### Exploit Mode File Selection

Unchanged from current behavior:
```
level0: repo-vul.tar.gz
level1: repo-vul.tar.gz, description.txt
level2: repo-vul.tar.gz, description.txt, error.txt
level3: repo-vul.tar.gz, repo-fix.tar.gz, error.txt, description.txt, patch.diff
```

---

## Data Flow with Phase 2

### Before Phase 2
```
CLI Input (task_id, out_dir, data_dir, server, difficulty)
        ↓
    gen_task()
        ↓
    generate_arvo_task()
        ↓
    prepare_arvo_files()
        ↓
    File Selection (DIFFICULTY_FILES only)
        ↓
    README + submit.sh (exploit mode)
        ↓
    Task Object (no evaluation_mode field)
```

### After Phase 2
```
CLI Input (task_id, out_dir, data_dir, server, difficulty, evaluation_mode)
        ↓
    gen_task()
        ↓
    generate_arvo_task()
        ↓
    prepare_arvo_files(evaluation_mode=...)
        ↓
    File Selection (conditional: RE vs exploit)
        ↓
    README + Template Selection (conditional)
        ↓
    Task Object (includes evaluation_mode + task_type)
```

---

## Backward Compatibility

All changes are **100% backward compatible**:

✅ `evaluation_mode` defaults to `"exploit"` everywhere
✅ Exploit mode behavior completely unchanged
✅ Existing code calling task generation works without modification
✅ Default CLI behavior identical to current
✅ No breaking changes to Task dataclass (new fields have defaults)

Test Case:
```bash
# This command works identically before and after Phase 2
gen_task --task-id arvo:10400 --out-dir /tmp/t1 --difficulty level1 --server http://localhost
```

---

## Testing Checklist

**Before Merging Phase 2:**

- [ ] Code Compilation
  - [ ] All Python files pass linting (flake8/pylint)
  - [ ] Type hints correct (mypy)
  - [ ] Imports resolve correctly

- [ ] Unit Tests
  - [ ] RE mode filters files correctly
  - [ ] Exploit mode behavior unchanged
  - [ ] Task object populated with evaluation_mode
  - [ ] Task object populated with task_type
  - [ ] File globs handle missing files gracefully

- [ ] Integration Tests
  - [ ] End-to-end RE task generation
  - [ ] End-to-end exploit task generation
  - [ ] RE mode: binary is always included
  - [ ] RE mode: source code never included
  - [ ] RE mode: hints included at correct levels
  - [ ] Both OSS-Fuzz generators updated correctly

- [ ] Manual Testing
  - [ ] `gen_task ... --evaluation-mode exploit` → exploit files
  - [ ] `gen_task ... --evaluation-mode reverse_engineering` → RE files
  - [ ] `gen_task ...` (no --evaluation-mode) → exploit files (default)
  - [ ] `gen_task ... --help` shows --evaluation-mode option

- [ ] Verification
  - [ ] Existing agents still work (unchanged behavior)
  - [ ] Checksum verification still works
  - [ ] Task serialization includes new fields
  - [ ] No data directory changes needed

---

## Integration with Other Phases

### Dependency Graph
```
Phase 1 (Data Models) ✅ COMPLETE
    ↓
Phase 2 (Task Generation) ← YOU ARE HERE
    ↓
    ├─→ Phase 3 (Submission Endpoint) - needs /submit-pseudocode
    ├─→ Phase 4 (Agent Runner) - needs evaluation_mode in Task
    └─→ Phase 5 (Prompts) - uses templates created here

Phase 6 (Judge Infrastructure) - independent, starts after Phase 1
    ↓
Phase 7 (Judge API Endpoints) - depends on Phase 6
```

### What Phase 3 Needs from Phase 2
- `/re_submit.sh` script that POSTs to `/submit-pseudocode` ✅ Provided
- Task object with `evaluation_mode` field ✅ Provided
- Checksum verification (reuses existing verify_task) ✅ Unchanged

### What Phase 4 Needs from Phase 2
- Task object with `evaluation_mode` field ✅ Provided
- Different prompt files (`prompt.txt` vs `prompt.re.txt`) - Phase 5 creates these
- Different submit scripts (`submit.sh` vs `re_submit.sh`) ✅ Provided

---

## Known Limitations & Future Considerations

### Not Handled by Phase 2
- Judge evaluation logic (Phase 6+)
- Submission endpoint implementation (Phase 3)
- Agent prompt customization (Phase 5)
- Binary extraction/compilation for RE datasets

### Future Enhancements
- Allow partial hints even in level0 (e.g., binary name hint)
- Support multiple binary formats (ELF, PE, Mach-O)
- Implement difficulty-specific optimization levels
- Add source file for judge (not to agent)

---

## Code Quality Metrics

| Metric | Value | Status |
|--------|-------|--------|
| Files Modified | 6 | ✅ Minimal |
| Lines Added | ~120 | ✅ Small |
| Breaking Changes | 0 | ✅ Backward Compatible |
| Test Coverage Required | ~90% | TODO |
| Documentation | Complete | ✅ Yes |
| Code Duplication | None | ✅ Clean |
| Circular Dependencies | None | ✅ Clean |

---

## Deployment Steps

1. **Stage 1: Code Review**
   - Review PHASE2_IMPLEMENTATION.md for design
   - Review code changes line-by-line
   - Verify backward compatibility

2. **Stage 2: Local Testing**
   - Run unit tests
   - Run integration tests
   - Manual testing of both modes

3. **Stage 3: Merge**
   - Merge to main branch
   - No immediate impact (Phase 3+ needed to use)

4. **Stage 4: Validation**
   - Smoke test existing exploit mode
   - Verify no regression

---

## Questions & Answers

**Q: Do I need new task data for RE mode?**
A: No. RE mode reuses existing ARVO/OSS-Fuzz binaries. The same `arvo:10400` works for both modes.

**Q: Can existing agents break?**
A: No. Exploit mode behavior is unchanged. Agents using exploit mode will work identically.

**Q: What if RE templates don't exist?**
A: Graceful fallback to exploit template with warning log. Non-blocking.

**Q: Do I need to update the agent runner?**
A: Only if you want agents to use RE mode. Phase 4 adds that capability.

**Q: When should I implement Phase 3?**
A: After Phase 2 is merged and tested. It depends on Phase 2's Task object changes.

---

## Contact & Support

For questions about Phase 2 implementation:
- See PHASE2_IMPLEMENTATION.md for detailed technical spec
- See testing checklist above for validation steps
- Review code diffs for exact changes

