Run brain quality metrics collection:
```bash
cd "${CASHEW_HOME}" && python3 scripts/brain-metrics.py
```
Then analyze trends:
```bash
cd "${CASHEW_HOME}" && python3 scripts/brain-metrics.py --report
```
Only report if there are concerning trends or significant anomalies. If metrics are stable and healthy, reply with just BRAIN_METRICS_COLLECTED.
