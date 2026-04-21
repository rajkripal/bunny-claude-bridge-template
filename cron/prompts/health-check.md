Run the brain graph health check:
```bash
cd "${CASHEW_HOME}" && KMP_DUPLICATE_LIB_OK=TRUE python3 scripts/cashew_context.py stats
```
The command must exit 0 and print real stats (node count, edge count, embedding coverage). If the command errors, exits non-zero, or the output doesn't contain stats, report the failure — do NOT reply OK. Otherwise, if node count is non-zero and embedding coverage is ≥95%, reply HEALTH_CHECK_OK. Anything else, report the details.
