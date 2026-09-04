# Claude 9,000 Trading Strategies Backtesting Suite: Complete Prompts & Specifications

This document contains the complete structural blueprint, architectural requirements, and the four copy-pasteable prompt layers used to build the automated quantitative backtesting and validation suite with Claude (or Claude Code).

---

## System Overview & Core Specifications

The architecture is designed to run over **9,000 backtests** across a diversified universe of assets over a **15-year period** (e.g., 2010–2025) to identify strategies with a genuine statistical edge, rather than those overfitted to historical noise [1, 2].

### 1. Ingestion Requirements (Asset Universe)
The system operates on **daily bar data** for liquid assets. Intraday, options, and futures data are excluded to keep execution boundaries conservative [2].
*   **Index ETFs:** `SPY` (S&P 500), `QQQ` (NASDAQ) [2]
*   **Sector ETFs:** Complete suite of US sectors (e.g., `XLK`, `XLF`, `XLE`, `XLV`, `XLI`, `XLY`, `XLP`, `XLB`, `XLU`, `XOP`, `KRE`) [2]
*   **Commodities & Alternatives:** `GLD` (Gold), `USO` (Crude Oil) [2]
*   **Fixed Income:** `TLT` (Long-Term Bonds), `IEF` (Medium-Term Bonds) [2]
*   **Cryptocurrencies:** `BTC-USD`, `ETH-USD` [2]
*   **Large-Cap Equities:** Strong individual trenders (e.g., `AAPL`, `NVDA`, `MSFT`, `AMZN`, `GOOGL`, `META`, `TSLA`, `NFLX`, `JPM`, `WMT`, `UNH`) [2]

### 2. The 6-Filter Validation Gauntlet
To weed out overfitted or weak strategies, all configurations are processed through six sequential filters, reducing over 9,000 initial tests to a robust set of survivors (historically leaving ~524-524 strategies) [4, 6]:
1.  **Walk-Forward Partitioning:** Builds and tunes parameters on an older historical block (In-Sample), then evaluates blindly on a newer block (Out-of-Sample) [4].
2.  **Sharpe Ratio Filter:** Demands an Out-of-Sample (OOS) Sharpe Ratio **> 0.5** on completely unseen data (removes ~80% of strategies) [4, 5].
3.  **Drawdown Threshold:** Discards any strategy experiencing a peak-to-trough drawdown **> 35%** [5].
4.  **Overfitting Guard:** Rejects strategies where In-Sample performance dramatically outpaces Out-of-Sample performance [6].
5.  **Profit Factor Filter:** Requires a minimum gross profit-to-loss ratio (typically > 1.1) on unseen data [6].
6.  **Trade Frequency Gate:** Rejects configurations with insufficient historical trade counts to prevent lucky statistical anomalies [6].

---

## The Four Prompt Layers

To build this application step-by-step without overloading the AI's context, the project is divided into four modular prompt layers. Copy and paste each prompt in sequence.

### Layer 1: Data and Strategy Library
**Purpose:** Initializes raw data handling, configures local caching, and builds base, unhedged strategy signals [21].

```markdown
You are an expert quantitative developer. I want to build Layer 1 of a modular, robust trading strategy backtesting system in Python. This layer is responsible for automated data ingestion and initializing our base strategy library. 

Please write a highly clean, production-ready, object-oriented Python codebase that implements the following requirements:

1. ASSET UNIVERSE & DATA ACQUISITION
Implement a data ingestion class `HistoricalDataManager` that downloads daily historical bar data (Open, High, Low, Close, Volume) using the `yfinance` library for the past 15 years (approx. 2010 to 2025) for the following 30 liquid assets:
- **Major Index ETFs:** SPY, QQQ
- **Sector ETFs:** XLK, XLF, XLE, XLV, XLI, XLY, XLP, XLB, XLU, XOP, KRE
- **Commodities & Alternatives:** GLD, USO
- **Fixed Income:** TLT, IEF
- **Cryptocurrencies:** BTC-USD, ETH-USD
- **Large-Cap Equities:** AAPL, NVDA, MSFT, AMZN, GOOGL, META, TSLA, NFLX, JPM, WMT, UNH

**Requirements:**
- Handle missing data, adjusted closes (to account for splits/dividends), and cache downloaded datasets locally in CSV format inside a `/data` folder to prevent redundant network calls.
- Include a method to cleanly slice data into train/test (in-sample vs. out-of-sample) windows for downstream walk-forward testing.

2. STRATEGY LIBRARY (THE STRATEGY BASE CLASS)
Create an abstract base class `BaseStrategy` with standard lifecycle hooks:
- `generate_signals(self, data)`: Returns a DataFrame of daily position signals (-1 for short, 0 for flat, 1 for long).
- Must be parameterized so that parameter sweeps can be run easily (e.g., changing RSI lengths, breakout lookbacks, etc.).

3. BASE STRATEGY IMPLEMENTATIONS (STRIPPED-DOWN & NAKED)
Implement the following classic trading strategies in their purest, naked forms (no extra stop losses or target profit rules yet; we want to test their raw mathematical edge):

A. Mean Reversion Family
- **RSI Snapback (`RSIReversion`):**
  - Parameters: `rsi_period` (default 14), `oversold_threshold` (default 30), `overbought_threshold` (default 70).
  - Signal: Buy (1) when RSI crosses below the oversold threshold and exit/short (-1) when it crosses above the overbought threshold.
- **Bollinger Band Reversion (`BBReversion`):**
  - Parameters: `period` (default 20), `num_std` (default 2).
  - Signal: Long (1) when price closes below the lower band, exit/short (-1) when it closes above the upper band.
- **Keltner Channel Reversion (`KeltnerReversion`):**
  - Parameters: `ema_period` (default 20), `atr_period` (default 10), `multiplier` (default 1.5).
  - Signal: Reversion bets when price stretches beyond the envelope limits.

B. Trend & Momentum Family
- **Moving Average Crossover (`MACrossover`):**
  - Parameters: `fast_period` (default 50), `slow_period` (default 200).
  - Signal: Golden Cross (1), Death Cross (-1).
- **Donchian Breakout / Turtle (`TurtleBreakout`):**
  - Parameters: `entry_period` (default 20), `exit_period` (default 10).
  - Signal: Long (1) on a breakout of the N-day high, exit (0) or short (-1) on a breakdown of the M-day low.
- **Single-Asset Momentum (`SimpleMomentum`):**
  - Parameters: `lookback_period` (default 126 days).
  - Signal: Long (1) if current price is above the price N days ago, short (-1) if below.

4. BATCH GENERATOR ARCHITECTURE
Write a script/runner structure that:
1. Loops through all 30 assets.
2. Loops through the strategies.
3. Performs a parameter grid definition for each strategy (e.g., testing RSI periods of and thresholds of [20/80, 30/70]).
4. Generates and stores the raw historical signals for all resulting configurations (this will scale up to the thousands of backtest permutations needed for Layer 2).

Ensure the code is modular, documented, handles errors gracefully, and utilizes standard vectorization via `pandas` and `numpy` for maximum performance.
```

---

### Layer 2: Walk-Forward Engine & Multi-Filter Funnel
**Purpose:** Handles rolling parameter optimization across walk-forward periods and runs the 6-stage gauntlet [21, 22].

```markdown
You are an expert quantitative developer. I want to build Layer 2 of our modular trading backtester in Python. This layer takes the daily price data, strategies, and raw signal matrices generated in Layer 1, processes them through a vectorized walk-forward execution engine, and runs them through a 6-stage validation funnel.

Please write a highly clean, production-ready Python codebase that implements the following requirements:

1. WALK-FORWARD ENGINE
Implement a walk-forward backtesting execution system (`WalkForwardEngine`) that dynamically simulates trading performance across rolling in-sample (IS) and out-of-sample (OOS) windows:
- **Time Setup:** Use a rolling or anchored window over our 15-year dataset. For example, train/tune parameters on a 3-year In-Sample period, and run the optimized parameters on the subsequent 1-year Out-of-Sample period. Shift the window forward by 1 year and repeat.
- **Transaction Costs & Slippage:** Crucial for realism. Build in customizable, execution drag parameters:
  - Commission: e.g., $0.005 per share or 0.05% of trade value.
  - Slippage: e.g., 0.02% price drag per execution.
- **Performance Calculator:** Calculate daily portfolio returns, cumulative returns, rolling drawdown, Sharpe ratio, and total trades executed.

2. THE 6-STAGE VALIDATION FUNNEL
Implement a validation funnel class (`ValidationFunnel`) that processes the results of all 9,000 backtests and drops failing configurations through successive filters:

- **Filter 1: Trade Viability (Min Trades)**
  - Reject any strategy configuration that has fewer than a minimum threshold of trades (e.g., minimum 30 trades over the backtest window) to ensure statistical relevance.
- **Filter 2: Out-of-Sample (OOS) Sharpe Ratio Filter**
  - Calculate the Sharpe ratio strictly on the Out-of-Sample (unseen) data.
  - Reject any strategy configuration that does not achieve an OOS Sharpe Ratio > 0.5.
- **Filter 3: Maximum Drawdown Limit**
  - Rejection Limit: 35% maximum drawdown peak-to-trough. If a strategy's OOS drawdown exceeds 35%, drop it.
- **Filter 4: Overfitting Check (IS vs. OOS Consistency)**
  - Compare the In-Sample performance (where parameters were tuned) against the Out-of-Sample performance (unseen data).
  - If the IS performance is dramatically higher than OOS performance (e.g., IS Sharpe is > 2x OOS Sharpe), flag and reject it as highly overfit to historical noise.
- **Filter 5: Profit Factor / Right-Tail Filter**
  - Require a minimum profit factor (gross profits divided by gross losses) greater than 1.1 on unseen data.
- **Filter 6: Multiple Comparison Correction (MCP / Luck Filter)**
  - Adjust the validation thresholds using a Bonferroni or False Discovery Rate (FDR) correction to account for the "p-hacking" effect of testing thousands of random parameter combinations.

3. METRICS & ANALYSIS OUTPUT
Generate the following structures to feed into our Layer 3 visualizer:
- A DataFrame summary of the survivors (including strategy name, asset ticker, IS Sharpe, OOS Sharpe, Max Drawdown, and Total Trades).
- Output coordinates to plot an "In-Sample vs. Out-of-Sample" scatter plot to visually isolate where overfitting killed the strategy.

Ensure the code is robust, fully vectorized for speed, and logs how many strategies survive each subsequent step of the funnel.
```

---

### Layer 3: Robustness Checks & Bootstrap Stress Testing
**Purpose:** Verifies surviving strategies using parameter sensitivity and 500x random shuffle bootstrapping [21, 23].

```markdown
You are an expert quantitative developer. I want to build Layer 3 of our modular trading backtesting system in Python. This layer takes the validated, surviving strategies from Layer 2 and subjects them to rigorous robustness tests: specifically, Parameter Sensitivity analysis and a Bootstrap Stress Test (Monte Carlo trade reshuffling).

Please write a clean, production-ready, object-oriented Python codebase implementing the following components:

1. PARAMETER SENSITIVITY TESTING
Create a module `ParameterSensitivityChecker` that evaluates how robust a surviving strategy configuration is to minor parameter changes:
- **Mechanics:** Take a surviving strategy's optimal parameter set (e.g., RSI Period = 14) and slightly perturb the parameter values (e.g., testing adjacent values: 12, 13, 15, 16).
- **Evaluation:**
  - Run backtests for these perturbed configurations.
  - Calculate the variance in performance (Sharpe, Returns, Max Drawdown) across this neighborhood.
  - Reject/flag any strategy where minor parameter changes cause the performance to collapse (indicating a fragile, overfit "parameter island" rather than a robust, stable regime).

2. BOOTSTRAP STRESS TESTER (TRADE RESHUFFLER)
Create a module `BootstrapStressTester` to stress-test the sequence of trades for each surviving strategy:
- **Mechanics:** 
  - Extract the exact list of individual trade returns (percentage wins/losses) generated by the strategy over the 15-year backtest.
  - Reshuffle (randomly sample with replacement) this exact sequence of trade returns **500 times** to simulate 500 "alternate universes" of how those exact trades could have played out chronologically.
- **Metrics to Track across the 500 runs:**
  - Standard Deviation of the final portfolio equity.
  - Worst-case (Max) Drawdown distribution. Identify if reshuffling causes the drawdown to spike catastrophically (e.g., dropping from 20% to over 50-60%, which indicates the original sequence relied on a highly specific, lucky path of the market).
  - Win/Loss sequence clusters (simulating consecutive loss streaks).
- **Verdict Rule:** Output a final robustness "Verdict" (e.g., Pass/Fail) based on whether the 5th percentile of the bootstrap equity curves still yields a positive return and keeps drawdown within an acceptable boundary.

3. INTEGRATION WITH SURVIVORS
Write an orchestration class `RobustnessSuite` that:
1. Ingests the `survivors_dataframe` output from Layer 2.
2. Runs both the Parameter Sensitivity Checker and the 500x Bootstrap Stress Tester on each surviving asset-strategy pair.
3. Appends two new metrics to the output: `sensitivity_score` (representing stability across parameters) and `bootstrap_verdict` (representing sequence-risk robustness).
4. Outputs a final CSV of "Ultra-Robust Strategies."

Make sure the bootstrap code uses NumPy's vectorized sampling features for efficient computation over 500 iterations, and write clear, well-commented Python classes.
```

---

### Layer 4: Market Regimes (HMM) & Dynamic Strategy Allocation
**Purpose:** Overlays a Hidden Markov Model regime filter, dynamic position sizing, and uncorrelated asset allocation [21].

```markdown
You are an expert quantitative developer and machine learning engineer. I want to build Layer 4—the final overlay—of our modular trading backtesting system in Python. This layer introduces market regime detection, dynamic position sizing, and a regime-switching portfolio execution engine.

Please write a highly clean, modular, and production-ready Python codebase that implements the following requirements:

1. MARKET REGIME CLASSIFIER (HIDDEN MARKOV MODEL)
Create a module `MarketRegimeDetector` that classifies the market state into distinct structural regimes using historical index data (e.g., SPY daily bars):
- **Features for Classification:** Compute daily features like:
  - 20-day rolling Volatility (standard deviation of daily returns).
  - 50-day over 200-day Simple Moving Average ratio (trend indicator).
  - Average True Range (ATR) normalized by price.
- **Classification Engine:** Implement a Hidden Markov Model (HMM) using `hmmlearn.hmm.GaussianHMM` to segment the market into 3 distinct hidden states:
  - State 0: High Volatility, Downward/Bearish Trend (Bear/Crash regime).
  - State 1: Low-to-Mid Volatility, Strong Upward/Bullish Trend (Trending regime).
  - State 2: Low Volatility, Mean-Reverting/Choppy (Ranging regime).
- **Interface:** Provide a `predict_regime(self, data)` method that appends a `regime` label (0, 1, or 2) to each trading day's timestamp.

2. RISK MANAGEMENT & POSITION SIZING ENGINE
Create a module `RiskManager` to handle trade-level risk controls and dynamic asset exposure:
- **ATR-Based Position Sizing:** Determine the dollar-amount exposure of each trade using a volatility-targeting approach (allocating smaller positions when daily ATR is abnormally high and larger positions when ATR is low to equalize risk across trades).
- **Hard Risk Gates:**
  - Introduce an Equity Curve Stop: Temporarily reduce risk or halt trading for a strategy if its rolling 20-day drawdown exceeds a designated threshold (e.g., 15%).
  - Maximum Portfolio Heat: Enforce a limit on total simultaneous portfolio exposure (e.g., maximum 300% total leverage across all asset-strategy allocations).

3. REGIME-SWITCHING PORTFOLIO ALLOCATOR
Create an orchestration class `DynamicRegimePortfolio` that dynamically routes capital based on the active market state:
- **State-Dependent Strategy Routing:** 
  - **In Regime 1 (Bullish/Trending):** Route capital preferentially to trend-following and momentum strategies (e.g., the Turtle breakout or Single-Asset momentum survivors identified in Layer 3).
  - **In Regime 2 (Choppy/Ranging):** Route capital exclusively to mean reversion strategies (e.g., RSI Snapback and Keltner reversion survivors).
  - **In Regime 0 (Bear/Crash):** Implement a defensive overlay—scaling down overall exposure, shifting to cash/short-term bonds (like TLT/IEF), or allowing selective short-biased mean reversion.
- **Uncorrelated Signal Blending:** Aggregate overlapping signals from active strategies on a given day, weighting them inversely to their historical correlation.

4. PERFORMANCE COMPARATOR RUNNER
Write an execution harness that:
1. Backtests the Dynamic Regime-Switched Portfolio over our 15-year historical dataset.
2. Backtests a Static Baseline Portfolio (such as running the best mean reversion strategies on autopilot 100% of the time, or a simple buy-and-hold portfolio).
3. Compares the two portfolios and outputs:
   - Annualized Return, Sharpe Ratio, Max Drawdown, and Calmar Ratio for both.
   - A rolling correlation matrix showing how strategy returns decoupled during transition periods.
   - A final combined CSV of trade executions, marked by the regime in which they were triggered.

Ensure all classes are fully documented, use robust vectorization via Pandas, NumPy, and Scikit-Learn, and handle edge cases (like regime transition lag) gracefully.
```

---

## Technical Integration with Claude Code

If you are using **Claude Code** locally rather than the Claude web interface, you can initialize your workspace with the following steps:

1.  **Initialize Git & Environment:**
    ```bash
    git init
    python3 -m venv venv
    source venv/bin/activate
    pip install pandas numpy yfinance scikit-learn hmmlearn matplotlib
    ```
2.  **File Structure Setup:**
    Create a clean repository layout for modular code generation:
    ```
    ├── data/                  # Local CSV price caches (Layer 1)
    ├── src/
    │   ├── __init__.py
    │   ├── data_loader.py    # HistoricalDataManager (Layer 1)
    │   ├── strategies.py     # BaseStrategy & subclasses (Layer 1)
    │   ├── backtest.py       # WalkForwardEngine & Funnel (Layer 2)
    │   ├── robustness.py     # Sensitivity & Bootstrap (Layer 3)
    │   └── regime.py         # HMM Classifier & Allocator (Layer 4)
    ├── main.py                # System runner and comparison output
    └── requirements.txt
    ```
3.  **Generating Files Incrementally:**
    Start Claude Code and execute commands referencing this file. For example:
    *   *“Claude, read the Layer 1 prompt from the MD guide and write `src/data_loader.py` and `src/strategies.py`.”*
    *   *“Now read the Layer 2 prompt and write `src/backtest.py` to integrate with the strategies.”*
