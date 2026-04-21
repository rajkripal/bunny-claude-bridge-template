Query the brain for open questions and pending items:
```bash
cd "${CASHEW_HOME}" && KMP_DUPLICATE_LIB_OK=TRUE python3 scripts/cashew_context.py context --hints "open questions pending unanswered follow-up action items"
```

For any items that surface:
- If answered but not acted on, note it
- If >24hrs with no response, flag as stale
- Extract any status changes back to the brain

If nothing needs attention, reply: QUESTIONS_OK
