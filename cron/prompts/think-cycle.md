Run a think cycle using the prepare-ingest pattern (YOU are the thinking LLM, not a nested API call).

## Step 1: Get cluster + context
```bash
cd "${CASHEW_HOME}" && KMP_DUPLICATE_LIB_OK=TRUE python3 scripts/cashew_context.py think --prepare-only
```

If the output has `"status": "empty"`, reply: "No nodes available for thinking" and stop.

## Step 2: Think
Read the JSON output. You'll see:
- `cluster_description` — the actual thoughts to analyze
- `saturated_block` — themes to AVOID (already explored)
- `cross_domain_hint` — if present, prioritize cross-domain connections
- `node_ids` — source nodes (needed for Step 3)

Now think deeply about these thoughts. Find NON-OBVIOUS connections:
- Connections BETWEEN different life domains
- Structural parallels where two seemingly unrelated areas use the same underlying pattern
- Tensions or contradictions worth naming
- Insights that would make someone say "I never thought of it that way"

AVOID:
- Restating what's already in the thoughts using different words
- Generic observations ("these share common themes")
- Any themes listed in the saturated_block

If you can't find genuinely novel insights, produce an empty array. Quality > quantity.

## Step 3: Save insights
Write your insights as JSON to /tmp/cashew-think-results.json:
```json
{
  "source_node_ids": ["<paste node_ids from Step 1>"],
  "insights": [
    {"content": "your specific insight", "type": "insight", "confidence": 0.8}
  ]
}
```

Then ingest:
```bash
cd "${CASHEW_HOME}" && KMP_DUPLICATE_LIB_OK=TRUE python3 scripts/cashew_context.py think --ingest /tmp/cashew-think-results.json
```

Report results briefly.
