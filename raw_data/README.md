# Raw ABCD source

These are unmodified files copied from the [ABCD upstream repository](https://github.com/asappresearch/abcd).
[`SOURCE.md`](SOURCE.md) records the upstream commit, license, file hashes,
and the files omitted from this project. The source is a fictional,
scripted/role-play retail support dataset, not real customer transactions or
live store records.

| File | Meaning in this project |
| --- | --- |
| `abcd_v1.1.json.gz` | Full source data with `train`, `dev`, and `test` splits. Each conversation has `convo_id`, `scenario`, `original`, and `delexed`. |
| `abcd_sample.json` | Small upstream example of the same conversation structure. |
| `guidelines.json` | Representative-facing workflow and policy text. This is the source of our policy draft. |
| `kb.json` | Subflow/action sequence index. It is not a customer or order database. |
| `ontology.json` | Intent, action, slot, and vocabulary definitions. It is not an agent trace or gold answer set. |

In a conversation, `scenario.personal`, `scenario.order`, and
`scenario.product` are background facts for the role play. `original` is the
recorded dialogue and action sequence. `delexed` normalizes parts of the
dialogue and includes action targets/parameters; those targets describe the
historical scripted representative's behavior. They do **not** prove that an
agent should take the same action at every earlier cutoff.

The eight selected request types contain **1,148 conversations but only 10
distinct fictional customer names** in `scenario.personal.customer_name`.
That is why the linked-customer database in `processed_data/` creates new
synthetic identities. The original files in this directory are never edited.
