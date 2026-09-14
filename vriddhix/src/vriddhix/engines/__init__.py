"""L3 -- the engines. THE SOURCE OF TRUTH.

Everything here is pure: DataFrame in, typed result out. No database, no HTTP,
no clock, no configuration read from a live process. The live scanner, the
backtester, the X-Ray and the research queries all call these same functions,
which is the only reason a backtest can be trusted to describe live behaviour.

`tests/test_engine_purity.py` enforces this by walking the AST of every module
in this package. It is not a style rule -- an engine that can reach live state
behaves differently in a backtest, and every number the system has ever
produced becomes suspect.
"""
