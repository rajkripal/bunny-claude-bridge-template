Review open TODOs in the brain, mark completed ones done, and flag overdue ones.

## Step 1: Get open TODOs
```bash
cd "${CASHEW_HOME}" && KMP_DUPLICATE_LIB_OK=TRUE python3 scripts/cashew_context.py context --hints "TODO pending action items commitments promises open tasks"
```

## Step 2: Check each TODO
For each open TODO, determine:
- **DONE** — evidence it's completed
- **OVERDUE** — older than 7 days with no progress
- **OPEN** — still active and on track
- **NOT_A_TODO** — miscategorized (skip)

## Step 3: Extract completions
For any DONE items, write to /tmp/cashew-extract-input.md and extract:
```bash
cd "${CASHEW_HOME}" && KMP_DUPLICATE_LIB_OK=TRUE python3 scripts/cashew_context.py extract --input /tmp/cashew-extract-input.md
```

## Step 4: Report
- If all handled: TODO_SQUASH_OK
- If overdue items exist: list them briefly
