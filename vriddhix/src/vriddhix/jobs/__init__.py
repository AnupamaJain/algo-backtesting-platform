"""L5 orchestration: the jobs that drive the engines and write their output.

Nothing here computes. Every number a job persists came from an engine in L3,
which is the rule that keeps the live scanner and the backtester in agreement.
"""
