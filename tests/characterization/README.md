# Characterization tests — Activation Report

Purpose: prove the report's output is **unchanged** across a refactor (e.g. adopting `truage-core`).
Deterministic: computes from a fixed `hubspot_pull.json` at a fixed `--asof`.

Workflow
1. **Baseline (before any change):**
   `python tests/characterization/chartest_activation.py snapshot --pull hubspot_pull.json --asof 2026-07-01 --out tests/characterization/baseline`
   (any real pull works — repo-root `hubspot_pull.json` or an `archive/` copy.)
2. Do the refactor.
3. **Candidate + gate:**
   `python tests/characterization/chartest_activation.py snapshot --pull hubspot_pull.json --asof 2026-07-01 --out tests/characterization/candidate`
   `python tests/characterization/chartest_activation.py compare tests/characterization/baseline tests/characterization/candidate`

`compare` exits non-zero and prints a field-level diff on any drift. Snapshot dirs are throwaway
(git-ignore them). Run from the repo root.
