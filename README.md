# 125 Final Project

A course project exploring a policy-guided retail customer service agent using the Action-Based Conversations Dataset (ABCD).

## Data

`raw_data/` contains unchanged ABCD source files. See [`raw_data/SOURCE.md`](raw_data/SOURCE.md) for the upstream commit, file hashes, license, and selection rationale.

The data processing plan and application design are being developed separately. Raw files should not be edited in place.

## Prepared test inputs

Run `python scripts/prepare_test_cases.py` to rebuild the unlabeled case pools and
case-isolated scenario fixtures in `prepared_data/`. See
[`prepared_data/README.md`](prepared_data/README.md) for their scope and limits.
