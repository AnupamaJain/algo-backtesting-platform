# Product Requirement Document (PRD)
## Quantitative Strategy Backtesting, Validation & Regime-Switching Engine

---

## 1. Product Overview & Core Objective
The objective of this product is to build an institutional-grade, multi-layered quantitative backtesting, validation, and execution system using **Claude Code** and Python. 

Most retail backtesting platforms (e.g., TradingView) lack rigorous validation protocols, leading to heavily overfit, fragile strategies that collapse when deployed in live markets. This system solves this issue by putting all strategy configurations through a **multi-stage validation gauntlet** (including out-of-sample walk-forward testing and Monte Carlo bootstrapping) and overlaying an **unsupervised machine learning classifier (Hidden Markov Model)** to dynamically switch trading rules based on the active market state.

---

## 2. Scope & Target Dataset
The system is designed for daily bar trading across liquid asset classes to evaluate long-term strategy durability over multiple market cycles.
*   **Time Horizon:** 15 years of daily historical data (approx. 2010 to 2025) to ensure strategies survive historic bull runs, choppy regimes, and catastrophic market crashes.
*   **Asset Universe (30 Liquid Assets):**
    *   *Major Index ETFs:* SPY, QQQ
    *   *Sector ETFs:* XLK, XLF, XLE, XLV, XLI, XLY, XLP, XLB, XLU, XOP, KRE
    *   *Commodities & Alternatives:* GLD, USO
    *   *Fixed Income:* TLT, IEF
    *   *Cryptocurrencies:* BTC-USD, ETH-USD
    *   *Large-Cap Equities:* AAPL, NVDA, MSFT, AMZN, GOOGL, META, TSLA, NFLX, JPM, WMT, UNH

---

## 3. Product Architecture (The Four-Layer Engine)

### **Layer 1: Data Ingestion & Strategy Library**
The foundation layer is responsible for gathering data and generating raw trading signals.
*   **Functional Requirements:**
    *   Automated historical daily bar data fetching (Open, High, Low, Close, Volume) utilizing the `yfinance` library.
    *   Local CSV caching in `/data` to prevent redundant network calls.
    *   Abstract Base Class `BaseStrategy` to define standard lifecycle hooks.
    *   Support for parameter sweeping (grid sweeps to scale up to 9,000 parallel backtest variations).
*   **Strategy Implementations (Bare, Naked Form):**
    *   *Mean Reversion:* RSI Snapback, Bollinger Band Reversion, Keltner Channel Reversion.
    *   *Trend/Momentum:* Moving Average Crossover, Donchian Breakout (Turtle System), Single-Asset Momentum.

### **Layer 2: Backtest Engine & Walk-Forward Funnel**
A strict vectorised execution engine built to simulate trading performance while correcting for parameter overfitting.
*   **Functional Requirements:**
    *   **Walk-Forward Analysis (WFA):** Divides data into rolling In-Sample (IS) tuning windows and Out-of-Sample (OOS) evaluation windows.
    *   **Trading Drag Simulation:** Integrates customizable transaction costs (e.g., $0.005/share) and execution slippage (e.g., 0.02% per fill).
*   **The 6-Stage Filter Gauntlet:**
    *   *Filter 1 (Trade Volume):* Drop configurations with fewer than 30 total trades to ensure statistical significance.
    *   *Filter 2 (Sharpe Hurdle):* Require an Out-of-Sample Sharpe Ratio > 0.5.
    *   *Filter 3 (Drawdown Cap):* Instantly eliminate any strategy with a peak-to-trough OOS drawdown > 35%.
    *   *Filter 4 (Overfitting Check):* Drop strategies where In-Sample performance is vastly superior (e.g., > 2x) to Out-of-Sample performance.
    *   *Filter 5 (Profit Factor):* Require a minimum OOS Profit Factor of 1.1.
    *   *Filter 6 (Multiple Comparison Correction):* Apply Bonferroni or FDR corrections to eliminate p-hacking/luck across the thousands of configurations.

### **Layer 3: Robustness Analysis & Stress Testing**
This layer validates whether a surviving strategy's performance was the result of a real market edge or simply a lucky chronological sequence of historical events.
*   **Functional Requirements:**
    *   **Parameter Sensitivity Testing:** Slightly perturbs optimal parameter values (e.g., moving RSI period from 14 to 13 or 15) to ensure the strategy is situated on a stable "performance plateau" rather than an overfit, fragile "parameter island."
    *   **Bootstrap Stress Test (Monte Carlo):** Reshuffles the exact chronological list of trade returns 500 times.
        *   If the reshuffled 5th percentile of equity curves experiences catastrophic drawdowns (e.g., >50%), the strategy is benched for high sequence-dependence.

### **Layer 4: ML Market Regimes & Portfolio Manager**
The dynamic allocation layer that serves as the "brain" of the portfolio, avoiding blind autopilot execution.
*   **Functional Requirements:**
    *   **Hidden Markov Model (HMM) Classifier:** Classifies the market daily into three hidden states using standard indicators (e.g., index volatility, 50/200 SMA ratios, normalized ATR):
        *   *State 0:* Bear Market / Crash (High Volatility, Downtrend).
        *   *State 1:* Bull Market / Trending (Low-to-Mid Volatility, Uptrend).
        *   *State 2:* Choppy / Ranging (Low Volatility, Sideways movement).
    *   **Dynamic Capital Routing:**
        *   During *Trending Regimes*, direct capital to robust Trend-Following and Cross-Sectional Momentum strategies (relative asset ranking: long strongest, short weakest).
        *   During *Ranging Regimes*, route capital strictly to Mean Reversion strategies (RSI, Bollinger Bands).
        *   During *Bear Regimes*, scale down total leverage, trigger selective short-biased signals, or rotate to cash/bonds (IEF, TLT).
    *   **Risk Management overlay:**
        *   *ATR-Based Position Sizing:* Normalizes risk across trades by sizing positions inversely to historical daily volatility.
        *   *Equity Curve Stop:* Temporarily benches or scales down capital allocation for any specific strategy whose rolling 20-day drawdown exceeds 15%.

---

## 4. Development Workflow with Claude Code
To compile this application systematically without running into large-context issues, use **Claude Code** to program, test, and write the application incrementally following this step-by-step pipeline:

```bash
# Step 1: Initialize the codebase directories
mkdir -p quant_backtester/{data,src,tests,results}
cd quant_backtester

# Step 2: Use Claude Code to build Layer 1 (Data Ingestion & Base Strategies)
# Command Claude: "Create a modular historical data manager using yfinance and write our BaseStrategy abstract class along with the RSI, BB, and Turtle implementations in src/strategies.py"

# Step 3: Use Claude Code to build Layer 2 (Walk-Forward Analysis & 6-Stage Filter Funnel)
# Command Claude: "Implement a vectorized walk-forward backtesting engine with realistic commission models and build the 6-stage validation funnel in src/engine.py"

# Step 4: Use Claude Code to build Layer 3 (Parameter Sensitivity & 500x Bootstrap Stress Tester)
# Command Claude: "Write a Monte Carlo trade reshuffler to simulate 500 alternate universe equity curves and add parameter perturbation logic in src/robustness.py"

# Step 5: Use Claude Code to build Layer 4 (HMM Regime Switching Classifier & ATR Position Sizer)
# Command Claude: "Integrate hmmlearn to create an unsupervised 3-state market classifier, build the ATR-based position sizer, and write the portfolio orchestration suite in src/portfolio.py"

# Step 6: Write unit tests to verify mathematical accuracy
# Command Claude: "Generate a complete suite of unit tests for our strategies, walk-forward calculator, and bootstrap reshuffler in tests/test_backtester.py"
```

---

## 5. Non-Functional Requirements & Performance Goals
*   **Vectorization:** Every performance calculation (returns, drawdown, indicators) must be fully vectorized using `pandas` and `numpy`. Avoid iterative `for` loops across rows to support massive batch sweeps.
*   **Modularity:** All four layers must communicate via standardized data schemas (standardized Pandas DataFrames with strict datetime indices).
*   **Stability:** If a network error occurs during a `yfinance` download, the data manager must fall back gracefully to local cached CSV files.
*   **Security:** Ensure that any downloaded dataset or file-system writing adheres strictly to local execution privileges, never exposing sensitive API endpoints.


1. Executive summary
Strategy Survival Lab is a browser-based teaching application that lets students explore why a strategy can look profitable in-sample yet fail on unseen data. It recreates the central learning journey shown in the reference video: run a strategy, pass it through a validation funnel, inspect risk-adjusted metrics, evaluate walk-forward performance, test sensitivity and path dependence, and compare survivors across assets.
The MVP uses deterministic, synthetic price series in the browser so every student sees a reproducible result without API keys, market-data licensing, or a backend. The interface should feel like a research workbench rather than a brokerage dashboard: the emphasis is on hypotheses, evidence, failure modes, and clear explanations.
2. Problem and learning goals
Students commonly learn a trading rule by looking at a single attractive equity curve. That approach hides overfitting, data leakage, unstable parameters, concentrated risk, and luck in the order of trades. The product solves this by making validation visible and interactive.
By the end of a guided session, a student should be able to explain the difference between in-sample and out-of-sample testing; calculate and interpret return, volatility, Sharpe ratio, win rate, and maximum drawdown; explain why walk-forward validation is more realistic than one static split; describe bootstrap reshuffling as a path-dependence check; distinguish a robust strategy from a fragile one; and propose a layered system using signal, sizing, diversification, and regime awareness.
3. Personas and use cases
Persona
Need
MVP outcome
Instructor
Demonstrate research concepts live
Can project the dashboard and reset to a known scenario
Beginner student
See abstract metrics become concrete
Can change parameters and observe validation results
Advanced student
Compare methods and failure modes
Can inspect charts, sensitivity, bootstrap, and cross-asset results
Curriculum designer
Teach a coherent sequence
Can use built-in lesson cards and discussion prompts
A typical use case is a 60–90 minute class in which the instructor begins with a strategy that appears strong in-sample, asks students to predict its out-of-sample result, runs the validation funnel, and then uses robustness checks to explain why the survivor score changes.
4. MVP product scope
4.1 Core screens
Screen / module
Purpose
Required interactions
Research Lab
Main dashboard and experiment controls
Select asset, strategy, train/test split, fast/slow periods, and run experiment
Validation Funnel
Make the research process explicit
Show pass/fail gates for data sufficiency, out-of-sample, drawdown, Sharpe, and stability
Performance
Explain return and risk
Toggle cumulative equity, price, drawdown, and trade markers
Walk-Forward
Teach temporal validation
Compare rolling train and test windows; highlight unseen segments
Robustness
Teach fragility and path dependence
Run parameter sensitivity and a deterministic bootstrap-style reshuffle visualization
Cross-Asset
Teach generalization
Compare the selected strategy across a small basket of assets
Lesson Mode
Support instruction
Show definitions, worked examples, questions, and next-step prompts
4.2 Supported educational strategies
The MVP should include two strategies so students can compare reasoning rather than memorize one formula.
Strategy
Educational role
Parameters
Moving Average Crossover
Demonstrates trend-following, lag, and parameter sensitivity
Fast period, slow period
RSI Mean Reversion
Demonstrates oversold/overbought logic and mean reversion
RSI period, oversold threshold, overbought threshold
The data should be synthetic but realistic-looking, with a trend segment, a choppy segment, and a sharp shock. This enables the app to illustrate regime changes without implying that results forecast live markets.
5. Functional requirements
Experiment engine
The engine must generate or load a deterministic OHLC-like close-price series, calculate strategy signals without look-ahead, apply a one-period execution delay, calculate daily or bar returns, and compound an equity curve. The engine must expose both in-sample and out-of-sample ranges.
Metrics
The app must calculate total return, annualized volatility, Sharpe ratio using a clearly stated zero risk-free rate for the classroom simulation, win rate, number of trades, maximum drawdown, and out-of-sample return. Each metric must have a short explanation available from the interface.
Validation funnel
The funnel must present gates in sequence. The recommended default gates are data sufficiency, no look-ahead warning, out-of-sample return greater than zero, maximum drawdown below 25%, Sharpe ratio above 0.75, and parameter stability above a minimum threshold. Each gate should show a status, the observed value, the threshold, and a short reason.
Walk-forward validation
The student can choose a split such as 60/40 or 70/30. The app must shade the training and testing portions of the chart and show the metric comparison. A rolling walk-forward view is a stretch goal; the MVP can use two or three sequential windows.
Robustness checks
Parameter sensitivity should evaluate a small grid around the selected periods and render a heatmap-like matrix of Sharpe scores. The bootstrap view should reshuffle the realized trade returns using a seeded pseudo-random generator and show a distribution of ending equity or maximum drawdown. The text must explain that reshuffling is a stress test, not proof of future performance.
Cross-asset comparison
The app should run the selected strategy over at least four synthetic assets representing different behaviors: broad-market trend, volatile growth, sideways range, and crypto-like high volatility. Results should be sortable by Sharpe, drawdown, or survival status.
Lesson mode
Lesson mode must include the sequence: question, definition, interactive action, observation, and reflection. It should contain instructor prompts such as “Which result would make you distrust the backtest?” and “What changed when we tested the same rule on another asset?”
Export and reset
The MVP must provide a “Reset classroom scenario” action and a “Copy experiment summary” action that copies a text summary to the clipboard. CSV/PDF export is out of scope for the first build.
6. Non-functional requirements
The application must work on current desktop Chromium and remain usable at tablet width. It should render without a server-side data dependency, load in under three seconds on a normal classroom connection, provide keyboard-visible focus states, use accessible labels, and respect reduced-motion preferences. All simulations must be deterministic after reset so an instructor can reproduce the same explanation.
7. Information architecture and visual direction
Use a dark research-console aesthetic with a warm paper-like lesson panel: deep navy background, electric cyan for observed data, amber for warnings, and mint for passing evidence. A persistent left rail should move between Lab, Funnel, Walk-Forward, Robustness, Cross-Asset, and Lesson Mode. The main canvas should prioritize one large chart, compact metric cards, and an evidence-oriented right rail. Use a modern sans-serif for UI and a restrained monospace accent for numerical readouts.
8. Technical approach for the MVP
The MVP is a client-only React application scaffolded with Vite, TypeScript, Tailwind CSS, and the provided component system. The data model consists of deterministic arrays generated in the browser. Strategy functions, metric functions, validation gates, and chart view models should be separated into small modules so students can inspect or extend them in a later coding lesson.
A production version would move historical-data acquisition, long-running backtests, authentication, saved experiments, and collaboration into a backend. It would also require a reviewed data policy, tests for signal timing, and stronger statistical methodology. Those capabilities are explicitly outside the MVP.
9. Success criteria
Area
MVP success measure
Concept comprehension
Students can correctly define out-of-sample testing, drawdown, Sharpe, and bootstrap stress testing after the lesson
Interaction
A student can run an experiment, change a parameter, and explain at least one changed metric
Robustness
The app visibly distinguishes a strong in-sample result from a weaker or unstable out-of-sample result
Instruction
An instructor can complete the guided flow in one class period without external data or configuration
Quality
No console errors in the main flow; charts and controls remain usable at desktop and tablet widths
10. Acceptance criteria
The app is ready for classroom review when a fresh load shows a complete default experiment; changing the strategy updates the metrics and charts; the train/test split is visibly different; at least one validation gate can fail and explain why; the robustness module produces a reproducible distribution; cross-asset rows update; Lesson Mode contains definitions and questions; and reset returns the app to the instructor scenario.
11. Risks and mitigations
The largest risk is that students interpret simulations as trading recommendations. Mitigate this with persistent educational-use language and synthetic data labeling. Another risk is accidental look-ahead bias in implementation. Mitigate it with an explicit one-period signal delay and a visible “execution delay” note. A third risk is false precision in bootstrap or Sharpe values. Mitigate it with plain-language caveats and a focus on comparative reasoning rather than thresholds as universal truths.
12. Future roadmap
Future versions may add Python-backed real-data imports, user accounts, saved notebooks, instructor scenario authoring, richer regime models, Monte Carlo confidence intervals, collaborative classrooms, and a code-view panel. These should be added only after the MVP demonstrates that students understand the validation funnel.
