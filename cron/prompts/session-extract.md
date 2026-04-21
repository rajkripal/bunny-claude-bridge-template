Session-to-brain extraction. The deterministic work (finding new entries
since last extraction, filtering to readable text) is done by a script.
Your job is the judgment layer: decide what's worth extracting.

**Step 1: Get new conversation since last watermark**
```bash
python3 "${SCRIPTS_HOME:-$HOME/scripts}/session-delta.py" > /tmp/session-delta.txt 2>/tmp/session-delta.err
```

Then check what the script emitted:
```bash
cat /tmp/session-delta.err
wc -l /tmp/session-delta.txt
```

If stderr shows `emitted=0`, there's nothing new — reply `EXTRACT_OK` and
stop. The script has already advanced its watermark appropriately; do not
re-run it.

**Step 2: Read the delta**
```bash
cat /tmp/session-delta.txt
```

The file contains plain-text turns prefixed by `--- USER @ <ts> ---` and
`--- ASSISTANT @ <ts> ---`. If the content is only mechanical exchanges
(acks, tool wiring, trivial back-and-forth), reply `EXTRACT_OK` and stop.

**Step 3: Write extraction input**
Pull out the substantive content — decisions, insights, corrections, new
context, commitments — and write it to a temp file:
```bash
cat > /tmp/cashew-extract-input.md << 'EOF'
<your curated summary of what's worth remembering from this delta>
EOF
```

**Step 4: Do LLM extraction**
Extract knowledge as JSON. Each item MUST include a domain field:
```json
[{"content": "specific knowledge", "type": "insight", "confidence": 0.7, "domain": "<user_domain>|<assistant_domain>"}]
```

**Domain assignment** — see CLAUDE.md for your project's domain split.

**Privacy tagging:**
- Tag `vault:private` for: personal finances, health, relationships, credentials, pre-launch IP

Write results to `/tmp/cashew-extract-results.json` and ingest:
```bash
cd "${CASHEW_HOME}" && KMP_DUPLICATE_LIB_OK=TRUE python3 scripts/cashew_context.py extract --ingest /tmp/cashew-extract-results.json
```

Quality over quantity — 3 good nodes beats 15 mediocre ones.
