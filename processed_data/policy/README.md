# Policy v1

[`policy_v1.json`](policy_v1.json) is the single source of truth for the
retail policy. The policy engine (`agent/policy/engine.py`) and the policy
gate read only this file. `scripts/render_policy_doc.py` renders it to
[`docs/POLICY_V1.md`](../../docs/POLICY_V1.md); edit the JSON, not the
rendered document.

Rules for the eight intents are transcribed from
[`raw_data/guidelines.json`](../../raw_data/guidelines.json), and each rule's
`provenance` gives the source path. General rules (identity, one customer per
conversation, confirmation, decline vs handoff) are modeled on the public
tau-bench retail policy. Choices not stated in either source are tagged
`assumption`.

This is a **project-authored transcription**, not policy JSON supplied by
ABCD. Its `status` remains `draft_for_review`.
