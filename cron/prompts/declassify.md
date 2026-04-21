Review vault:private nodes for possible declassification.

**Step 1: Get candidates**
```bash
cd "${CASHEW_HOME}" && python3 scripts/declassify.py --candidates
```

**Step 2: Reason about each candidate.** For each node, decide: declassify or keep private.

DECLASSIFICATION RULES (apply in order, and customize for your threat model):
1. NEVER declassify: personal life, relationships, family, health, finances
2. NEVER declassify: architecture/algorithms for pre-launch IP you haven't published
3. NEVER declassify: credentials, API keys, addresses, account details
4. NEVER declassify: private conversations, DM content, things said in confidence
5. CAN declassify: general engineering principles that aren't proprietary
6. CAN declassify: public information already shared openly
7. CAN declassify: operational patterns that reveal no private info
8. When in doubt: keep private. False negatives are fine, false positives are not.

**Step 3: Apply decisions** — pass ONLY the safe IDs:
```bash
cd "${CASHEW_HOME}" && python3 scripts/declassify.py --declassify-ids <id1> <id2> ...
```

If no candidates or nothing safe to declassify, reply: DECLASSIFY_OK
