### PROMPT TO CLAUDE CODE

We are building an algorithmic trading research and backtesting platform.

The goal is to create reliable software for researching trading strategies.

This is NOT a request to create a guaranteed-profitable trading strategy.

The first milestone is a minimal working backtesting engine.

The system should eventually support:

1. Historical market data
2. Strategy definitions
3. Signal generation
4. Simulated order execution
5. Position tracking
6. Portfolio management
7. Transaction costs
8. Slippage
9. P&L calculation
10. Equity curves
11. Drawdown analysis
12. Performance metrics
13. In-sample and out-of-sample testing
14. Walk-forward testing
15. Paper trading in a later phase

Architecture requirements:

* Strategy logic must be separated from the backtesting engine.
* Strategies must not contain broker-specific code.
* Market data must be separated from strategy logic.
* Execution must be abstracted.
* Portfolio and position management must be separate components.
* Configuration must be separated from code.
* The system must be testable.
* Backtests must be reproducible.
* The design must explicitly protect against look-ahead bias.

Before writing implementation code:

1. Inspect the project.
2. Propose the folder structure.
3. Explain the architecture.
4. Define the core interfaces.
5. Explain the data flow.
6. Identify potential sources of look-ahead bias.
7. Define the first minimal vertical slice.

Do not implement everything at once.

Start with the architecture and wait for approval.
