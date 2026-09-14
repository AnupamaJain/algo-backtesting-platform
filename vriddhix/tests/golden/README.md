# Golden datasets

Small, committed CSVs with their expected engine outputs alongside. Generated
once, verified by hand, then frozen: a golden file changes only in a commit
that explains why the correct answer changed.

Empty in Phase 1 -- there are no engines yet to have golden outputs. Populated
in Phase 2 with the VCP cases named in `docs/10-test-strategy.md §3`:

    vcp_textbook        a clean 4-contraction base
    vcp_imperfect       18 -> 11 -> 12 -> 6, must still score well
    vcp_too_deep        45% base, must be rejected
    vcp_no_prior_trend  base after a downtrend, must be rejected

Feature-level correctness is covered instead by hand-computed assertions in
`test_features.py` and by the causality suite running against real NSE history.
