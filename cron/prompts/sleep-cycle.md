Run the brain sleep protocol:
```bash
cd "${CASHEW_HOME}" && KMP_DUPLICATE_LIB_OK=TRUE python3 scripts/cashew_context.py --debug sleep
```
Report the full output including debug diagnostics. If anything interesting happened (new cross-links, unexpected demotions, dream nodes), note it. Otherwise just confirm it ran.

Then redeploy the dashboard so it reflects the latest graph:
```bash
cd "${CASHEW_HOME}" && bash scripts/deploy-dashboard.sh
```
Report deploy success/failure briefly.
