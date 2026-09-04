/**
 * Declarative schema for every user-editable pipeline setting.
 *
 * This one definition drives three things: the form controls the UI renders,
 * the help text a user reads, and the validation the server enforces before
 * anything is written to disk. Keeping them in a single source means the UI
 * can never offer an edit the backend would reject, and no field can be
 * silently written without bounds.
 *
 * Anything NOT listed here cannot be modified through the UI, by design —
 * the API refuses unknown paths rather than trusting the client.
 */

export type FieldType =
  | "int"
  | "float"
  | "bool"
  | "text"
  | "enum"
  | "intList"
  | "floatList"
  | "stringList";

export type Field = {
  /** Path into the YAML document, e.g. ["funnel", "filter_1_min_trades"]. */
  path: (string | number)[];
  label: string;
  help: string;
  type: FieldType;
  min?: number;
  max?: number;
  step?: number;
  options?: string[];
  /** Rendered as a percentage input but stored as a fraction. */
  asPercent?: boolean;
};

export type Group = { title: string; description: string; fields: Field[] };
export type ConfigFile = { file: string; layer: number; groups: Group[] };

export const STRATEGY_NAMES = [
  // Mean reversion
  "RSIReversion",
  "BBReversion",
  "KeltnerReversion",
  "ZScoreReversion",
  "WilliamsRReversion",
  "CCIReversion",
  // Trend
  "MACrossover",
  "MACDTrend",
  "SupertrendFollow",
  "ADXTrend",
  // Momentum
  "SimpleMomentum",
  "ROCMomentum",
  "DualMomentum",
  // Breakout
  "TurtleBreakout",
  "VolatilityBreakout",
  "SqueezeBreakout",
  // Volatility
  "VolatilityRegime",
  "VolatilityMeanReversion",
  // Chart patterns
  "StructureBreak",
  "InsideBarBreakout",
  "EngulfingReversal",
] as const;

// -- Layer 1 ------------------------------------------------------------

const universeConfig: ConfigFile = {
  file: "universe.yaml",
  layer: 1,
  groups: [
    {
      title: "Data window",
      description: "How much history to download and test over.",
      fields: [
        {
          path: ["data", "lookback_years"],
          label: "Lookback years",
          help: "Used only when no explicit start date is set. 15 years spans multiple bull, bear and choppy cycles.",
          type: "int",
          min: 1,
          max: 40,
        },
        {
          path: ["data", "start_date"],
          label: "Start date",
          help: "ISO date (YYYY-MM-DD). Leave blank to derive it from lookback years.",
          type: "text",
        },
        {
          path: ["data", "end_date"],
          label: "End date",
          help: "ISO date (YYYY-MM-DD). Leave blank to use today.",
          type: "text",
        },
        {
          path: ["data", "max_forward_fill_days"],
          label: "Max forward-fill days",
          help: "How many consecutive missing days may be filled forward before rows are dropped instead.",
          type: "int",
          min: 0,
          max: 30,
        },
      ],
    },
    {
      title: "Asset universe",
      description:
        "The symbols the whole pipeline runs over. Each group is a comma-separated ticker list.",
      fields: [
        { path: ["assets", "index_etfs"], label: "Index ETFs", help: "Broad market index trackers.", type: "stringList" },
        { path: ["assets", "sector_etfs"], label: "Sector ETFs", help: "Sector-level exposure.", type: "stringList" },
        { path: ["assets", "commodities"], label: "Commodities", help: "Commodity and alternative exposure.", type: "stringList" },
        { path: ["assets", "fixed_income"], label: "Fixed income", help: "Bond ETFs — also used as defensive assets in Layer 4.", type: "stringList" },
        { path: ["assets", "crypto"], label: "Crypto", help: "Crypto pairs. Note these trade 7 days a week, unlike the equity assets.", type: "stringList" },
        { path: ["assets", "large_cap_equities"], label: "Large-cap equities", help: "Individual large-cap names.", type: "stringList" },
      ],
    },
  ],
};

const strategyGridConfig: ConfigFile = {
  file: "strategy_grid.yaml",
  layer: 1,
  groups: [
    {
      title: "RSI Snapback",
      description: "Mean reversion: buy oversold, flip short when overbought.",
      fields: [
        { path: ["strategies", "RSIReversion", "rsi_period"], label: "RSI period", help: "Lookback for the RSI calculation.", type: "intList" },
        { path: ["strategies", "RSIReversion", "oversold_threshold"], label: "Oversold threshold", help: "RSI level that triggers a long.", type: "intList" },
        { path: ["strategies", "RSIReversion", "overbought_threshold"], label: "Overbought threshold", help: "RSI level that flips to short.", type: "intList" },
      ],
    },
    {
      title: "Bollinger Band Reversion",
      description: "Mean reversion against a volatility envelope.",
      fields: [
        { path: ["strategies", "BBReversion", "period"], label: "Period", help: "Moving-average window for the band centre.", type: "intList" },
        { path: ["strategies", "BBReversion", "num_std"], label: "Std deviations", help: "Band width in standard deviations.", type: "floatList" },
      ],
    },
    {
      title: "Keltner Channel Reversion",
      description: "Mean reversion against an ATR-based envelope.",
      fields: [
        { path: ["strategies", "KeltnerReversion", "ema_period"], label: "EMA period", help: "Channel centre line.", type: "intList" },
        { path: ["strategies", "KeltnerReversion", "atr_period"], label: "ATR period", help: "Window for the ATR band width.", type: "intList" },
        { path: ["strategies", "KeltnerReversion", "multiplier"], label: "ATR multiplier", help: "How many ATRs wide the channel is.", type: "floatList" },
      ],
    },
    {
      title: "Moving Average Crossover",
      description: "Trend following: long above the slow MA, short below.",
      fields: [
        { path: ["strategies", "MACrossover", "fast_period"], label: "Fast period", help: "Must be shorter than the slow period.", type: "intList" },
        { path: ["strategies", "MACrossover", "slow_period"], label: "Slow period", help: "The classic golden/death cross uses 50 and 200.", type: "intList" },
      ],
    },
    {
      title: "Donchian Breakout (Turtle)",
      description: "Trend following: enter on N-day breakouts.",
      fields: [
        { path: ["strategies", "TurtleBreakout", "entry_period"], label: "Entry period", help: "Breakout channel for entries.", type: "intList" },
        { path: ["strategies", "TurtleBreakout", "exit_period"], label: "Exit period", help: "Usually shorter than the entry channel.", type: "intList" },
      ],
    },
    {
      title: "Single-Asset Momentum",
      description: "Long if price is above its level N days ago.",
      fields: [
        { path: ["strategies", "SimpleMomentum", "lookback_period"], label: "Lookback days", help: "126 trading days is roughly six months.", type: "intList" },
      ],
    },
    // -- Mean reversion additions ------------------------------------------
    {
      title: "Z-Score Reversion",
      description: "Fade statistically extreme deviations from a rolling mean.",
      fields: [
        { path: ["strategies", "ZScoreReversion", "period"], label: "Period", help: "Rolling window for the mean and standard deviation.", type: "intList" },
        { path: ["strategies", "ZScoreReversion", "entry_z"], label: "Entry Z-score", help: "How many standard deviations from the mean triggers an entry.", type: "floatList" },
      ],
    },
    {
      title: "Williams %R Reversion",
      description: "Oversold/overbought reversion using Williams %R.",
      fields: [
        { path: ["strategies", "WilliamsRReversion", "period"], label: "Period", help: "Lookback window for Williams %R.", type: "intList" },
        { path: ["strategies", "WilliamsRReversion", "oversold"], label: "Oversold level", help: "Williams %R below this triggers a long (e.g. -80).", type: "intList" },
        { path: ["strategies", "WilliamsRReversion", "overbought"], label: "Overbought level", help: "Williams %R above this flips to short (e.g. -20).", type: "intList" },
      ],
    },
    {
      title: "CCI Reversion",
      description: "Fade Commodity Channel Index extremes.",
      fields: [
        { path: ["strategies", "CCIReversion", "period"], label: "Period", help: "Lookback window for the CCI calculation.", type: "intList" },
        { path: ["strategies", "CCIReversion", "threshold"], label: "Threshold", help: "CCI level (±) at which entries are triggered. Typical: 100.", type: "floatList" },
      ],
    },
    // -- Trend additions ---------------------------------------------------
    {
      title: "MACD Trend",
      description: "Long while the MACD line leads its signal line.",
      fields: [
        { path: ["strategies", "MACDTrend", "fast"], label: "Fast EMA", help: "Short exponential average period.", type: "intList" },
        { path: ["strategies", "MACDTrend", "slow"], label: "Slow EMA", help: "Long exponential average period.", type: "intList" },
        { path: ["strategies", "MACDTrend", "signal"], label: "Signal line", help: "Smoothing period for the MACD signal line.", type: "intList" },
      ],
    },
    {
      title: "Supertrend Follow",
      description: "Follow the Supertrend band direction.",
      fields: [
        { path: ["strategies", "SupertrendFollow", "atr_period"], label: "ATR period", help: "Window for the Average True Range used to set band width.", type: "intList" },
        { path: ["strategies", "SupertrendFollow", "multiplier"], label: "ATR multiplier", help: "How many ATRs wide the band is. Higher = fewer flips.", type: "floatList" },
      ],
    },
    {
      title: "ADX Trend Filter",
      description: "Trade in the +DI/-DI direction only when trend strength exceeds a threshold.",
      fields: [
        { path: ["strategies", "ADXTrend", "period"], label: "Period", help: "Lookback for ADX and directional index.", type: "intList" },
        { path: ["strategies", "ADXTrend", "adx_threshold"], label: "ADX threshold", help: "ADX must exceed this for a position to be taken. Classic: 25.", type: "floatList" },
      ],
    },
    // -- Momentum additions ------------------------------------------------
    {
      title: "Rate-of-Change Momentum",
      description: "Long when trailing return clears a threshold, short when it falls below.",
      fields: [
        { path: ["strategies", "ROCMomentum", "period"], label: "Period", help: "Lookback for the rate-of-change calculation.", type: "intList" },
        { path: ["strategies", "ROCMomentum", "threshold"], label: "Threshold", help: "Minimum return magnitude to take a position. Dead-bands noise around zero.", type: "floatList" },
      ],
    },
    {
      title: "Dual Momentum",
      description: "Require agreement between a short and a long lookback before acting.",
      fields: [
        { path: ["strategies", "DualMomentum", "short_period"], label: "Short lookback", help: "Near-term momentum window.", type: "intList" },
        { path: ["strategies", "DualMomentum", "long_period"], label: "Long lookback", help: "Must be longer than the short lookback.", type: "intList" },
      ],
    },
    // -- Breakout additions ------------------------------------------------
    {
      title: "Volatility Breakout",
      description: "Enter when price travels more than N × ATR from the prior close.",
      fields: [
        { path: ["strategies", "VolatilityBreakout", "atr_period"], label: "ATR period", help: "Window for ATR calculation.", type: "intList" },
        { path: ["strategies", "VolatilityBreakout", "multiplier"], label: "ATR multiplier", help: "Minimum move in ATR units required to enter.", type: "floatList" },
      ],
    },
    {
      title: "Squeeze Breakout",
      description: "Trade the expansion that follows a Bollinger Band volatility squeeze.",
      fields: [
        { path: ["strategies", "SqueezeBreakout", "period"], label: "BB period", help: "Bollinger Band window.", type: "intList" },
        { path: ["strategies", "SqueezeBreakout", "num_std"], label: "Std deviations", help: "Bollinger Band width.", type: "floatList" },
        { path: ["strategies", "SqueezeBreakout", "squeeze_lookback"], label: "Squeeze lookback", help: "How many bars back to measure bandwidth against.", type: "intList" },
        { path: ["strategies", "SqueezeBreakout", "squeeze_quantile"], label: "Squeeze quantile", help: "Bandwidth must be in this bottom quantile of history to qualify as a squeeze.", type: "floatList" },
      ],
    },
    // -- Volatility --------------------------------------------------------
    {
      title: "Volatility Regime",
      description: "Hold risk only while realized volatility is subdued relative to its own history. The family that drove 180 of 220 ultra-robust survivors.",
      fields: [
        { path: ["strategies", "VolatilityRegime", "vol_period"], label: "Vol period", help: "Lookback for realized volatility.", type: "intList" },
        { path: ["strategies", "VolatilityRegime", "calm_quantile"], label: "Calm quantile", help: "Volatility must be below this historical quantile to stay long.", type: "floatList" },
        { path: ["strategies", "VolatilityRegime", "lookback"], label: "Quantile lookback", help: "Rolling history used to define the calm threshold.", type: "intList" },
      ],
    },
    {
      title: "Volatility Mean Reversion",
      description: "Buy after a volatility spike exhausts itself.",
      fields: [
        { path: ["strategies", "VolatilityMeanReversion", "vol_period"], label: "Vol period", help: "Lookback for realized volatility.", type: "intList" },
        { path: ["strategies", "VolatilityMeanReversion", "spike_quantile"], label: "Spike quantile", help: "Vol must exceed this quantile of history to flag a spike.", type: "floatList" },
        { path: ["strategies", "VolatilityMeanReversion", "lookback"], label: "Quantile lookback", help: "Rolling history used to define the spike threshold.", type: "intList" },
      ],
    },
    // -- Chart patterns ----------------------------------------------------
    {
      title: "Structure Break",
      description: "Trade market structure: higher highs vs lower lows.",
      fields: [
        { path: ["strategies", "StructureBreak", "swing_period"], label: "Swing period", help: "Lookback for prior swing high/low.", type: "intList" },
      ],
    },
    {
      title: "Inside Bar Breakout",
      description: "An inside bar marks compression; trade the break of its range.",
      fields: [
        { path: ["strategies", "InsideBarBreakout", "confirm_bars"], label: "Confirm bars", help: "Bars after the inside bar before confirming a breakout.", type: "intList" },
      ],
    },
    {
      title: "Engulfing Reversal",
      description: "Bullish/bearish engulfing candle patterns, optionally filtered by trend.",
      fields: [
        { path: ["strategies", "EngulfingReversal", "trend_period"], label: "Trend period", help: "MA period used to determine prevailing direction.", type: "intList" },
        { path: ["strategies", "EngulfingReversal", "require_trend"], label: "Require trend filter", help: "1 = only take engulfing candles against the trend (reversal); 0 = trade all engulfing candles.", type: "intList" },
      ],
    },
  ],
};

// -- Layer 2 ------------------------------------------------------------

const backtestConfig: ConfigFile = {
  file: "backtest.yaml",
  layer: 2,
  groups: [
    {
      title: "Execution costs",
      description:
        "Trading drag applied to every fill. Understating costs is the most common way a backtest flatters a strategy.",
      fields: [
        {
          path: ["costs", "model"],
          label: "Cost model",
          help: "Percentage charges a share of notional; per-share charges by share count, which penalises low-priced assets more.",
          type: "enum",
          options: ["percentage", "per_share"],
        },
        {
          path: ["costs", "percentage", "commission_pct"],
          label: "Commission",
          help: "Percentage of traded notional charged per fill.",
          type: "float",
          min: 0,
          max: 0.05,
          step: 0.0001,
          asPercent: true,
        },
        {
          path: ["costs", "percentage", "slippage_pct"],
          label: "Slippage",
          help: "Price drag per execution, as a percentage of notional.",
          type: "float",
          min: 0,
          max: 0.05,
          step: 0.0001,
          asPercent: true,
        },
        {
          path: ["costs", "per_share", "commission_per_share"],
          label: "Commission per share ($)",
          help: "Only used when the per-share cost model is selected.",
          type: "float",
          min: 0,
          max: 1,
          step: 0.001,
        },
      ],
    },
    {
      title: "Walk-forward windows",
      description:
        "Parameters are tuned on the in-sample window, then judged on the untouched out-of-sample window that follows it.",
      fields: [
        {
          path: ["walk_forward", "mode"],
          label: "Window mode",
          help: "Rolling slides a fixed-length training window forward; anchored keeps the start pinned and grows it.",
          type: "enum",
          options: ["rolling", "anchored"],
        },
        { path: ["walk_forward", "is_years"], label: "In-sample years", help: "Length of each tuning window.", type: "int", min: 1, max: 10 },
        { path: ["walk_forward", "oos_years"], label: "Out-of-sample years", help: "Length of each evaluation window.", type: "int", min: 1, max: 5 },
        { path: ["walk_forward", "step_years"], label: "Step years", help: "How far the window advances each iteration.", type: "int", min: 1, max: 5 },
        { path: ["walk_forward", "min_oos_days"], label: "Min OOS days", help: "Windows with less out-of-sample data than this are skipped.", type: "int", min: 5, max: 500 },
      ],
    },
    {
      title: "Validation funnel thresholds",
      description:
        "The six gates every configuration must clear. Loosening these lets more strategies through — including ones that only look good by luck.",
      fields: [
        {
          path: ["funnel", "filter_1_min_trades"],
          label: "1 · Minimum trades",
          help: "Too few trades makes every other metric statistical noise, however good it looks.",
          type: "int",
          min: 1,
          max: 500,
        },
        {
          path: ["funnel", "filter_2_min_oos_sharpe"],
          label: "2 · Minimum OOS Sharpe",
          help: "Risk-adjusted return required on data the parameters never saw.",
          type: "float",
          min: -2,
          max: 5,
          step: 0.05,
        },
        {
          path: ["funnel", "filter_3_max_drawdown"],
          label: "3 · Maximum drawdown",
          help: "Peak-to-trough loss a strategy may show out-of-sample before being dropped.",
          type: "float",
          min: 0.01,
          max: 1,
          step: 0.01,
          asPercent: true,
        },
        {
          path: ["funnel", "filter_4_max_is_oos_sharpe_ratio"],
          label: "4 · Max IS/OOS Sharpe ratio",
          help: "Rejects overfitting: an in-sample Sharpe far above the out-of-sample one means the parameters were fitted to noise.",
          type: "float",
          min: 1,
          max: 10,
          step: 0.1,
        },
        {
          path: ["funnel", "filter_5_min_profit_factor"],
          label: "5 · Minimum profit factor",
          help: "Gross wins divided by gross losses on unseen data. 1.0 is break-even.",
          type: "float",
          min: 0.5,
          max: 5,
          step: 0.05,
        },
        {
          path: ["funnel", "filter_6", "method"],
          label: "6 · Multiple-comparison method",
          help: "Corrects for testing thousands of configurations. Bonferroni is stricter; FDR is more permissive.",
          type: "enum",
          options: ["fdr", "bonferroni"],
        },
        {
          path: ["funnel", "filter_6", "alpha"],
          label: "6 · Significance level (alpha)",
          help: "Tolerated false-positive rate before correction.",
          type: "float",
          min: 0.001,
          max: 0.5,
          step: 0.005,
        },
        {
          path: ["funnel", "filter_6", "multiplicity"],
          label: "6 · Hypothesis count",
          help: "'strategies' counts 21 distinct strategy types — the setting that found 248 survivors. 'clustered' counts symbol×strategy pairs (~630), making the correction ~30× stricter. 'all' counts every configuration tested (~10,470), making it nearly impossible to pass.",
          type: "enum",
          options: ["strategies", "clustered", "all"],
        },
      ],
    },
    {
      title: "Return assumptions",
      description: "Conventions used when annualizing performance.",
      fields: [
        { path: ["execution", "periods_per_year"], label: "Periods per year", help: "252 trading days for daily bars.", type: "int", min: 1, max: 366 },
        {
          path: ["execution", "risk_free_rate"],
          label: "Risk-free rate",
          help: "Annualized rate subtracted when computing Sharpe.",
          type: "float",
          min: 0,
          max: 0.2,
          step: 0.001,
          asPercent: true,
        },
      ],
    },
  ],
};

// -- Layer 3 ------------------------------------------------------------

const robustnessConfig: ConfigFile = {
  file: "robustness.yaml",
  layer: 3,
  groups: [
    {
      title: "Parameter sensitivity",
      description:
        "Nudges each parameter to neighbouring values. A robust strategy sits on a plateau where nearby settings also work; an overfit one is a fragile spike.",
      fields: [
        { path: ["sensitivity", "int_offsets"], label: "Integer offsets", help: "Whole-unit steps tested around each integer parameter, e.g. RSI 14 → 12, 13, 15, 16.", type: "intList" },
        { path: ["sensitivity", "float_relative_offsets"], label: "Float offsets", help: "Relative steps for decimal parameters, as fractions (0.1 = ±10%).", type: "floatList" },
        { path: ["sensitivity", "metric"], label: "Judged on", help: "Which out-of-sample metric the neighbourhood is compared on.", type: "enum", options: ["sharpe", "total_return", "cagr", "profit_factor"] },
        {
          path: ["sensitivity", "min_retention_ratio"],
          label: "Min retention ratio",
          help: "Fraction of the centre's performance a neighbour must retain to count as holding up.",
          type: "float",
          min: 0,
          max: 1,
          step: 0.05,
        },
        {
          path: ["sensitivity", "min_stability_fraction"],
          label: "Min stability fraction",
          help: "Share of neighbours that must hold up for the configuration to be called stable.",
          type: "float",
          min: 0,
          max: 1,
          step: 0.05,
        },
      ],
    },
    {
      title: "Bootstrap stress test",
      description:
        "Reshuffles the exact realized trades many times. If drawdown explodes under reshuffling, the result depended on a lucky ordering rather than an edge.",
      fields: [
        { path: ["bootstrap", "num_simulations"], label: "Simulations", help: "Number of alternate-universe trade orderings to build.", type: "int", min: 50, max: 10000 },
        { path: ["bootstrap", "random_seed"], label: "Random seed", help: "Fixed so verdicts are reproducible. Change deliberately, never incidentally.", type: "int", min: 0, max: 999999 },
        { path: ["bootstrap", "with_replacement"], label: "Sample with replacement", help: "On: trades may repeat, so final equity varies. Off: pure reordering of the same trades.", type: "bool" },
        { path: ["bootstrap", "trade_source"], label: "Trade source", help: "Which track record to resample — the honest out-of-sample one, or full history.", type: "enum", options: ["oos", "full"] },
        { path: ["bootstrap", "min_trades_required"], label: "Min trades required", help: "Below this the bootstrap refuses to render a verdict rather than guess.", type: "int", min: 5, max: 200 },
        {
          path: ["bootstrap", "min_p5_final_return"],
          label: "Min 5th-percentile return",
          help: "The unlucky-but-plausible universe must still make at least this much.",
          type: "float",
          min: -1,
          max: 2,
          step: 0.01,
          asPercent: true,
        },
        {
          path: ["bootstrap", "max_p95_drawdown"],
          label: "Max 95th-percentile drawdown",
          help: "Drawdown ceiling in a bad-but-plausible reshuffle.",
          type: "float",
          min: 0.05,
          max: 1,
          step: 0.01,
          asPercent: true,
        },
      ],
    },
  ],
};

// -- Layer 4 ------------------------------------------------------------

const regimeConfig: ConfigFile = {
  file: "regime.yaml",
  layer: 4,
  groups: [
    {
      title: "Regime detection (HMM)",
      description:
        "An unsupervised Hidden Markov Model segments market history into bear, trending and ranging states from volatility and trend features.",
      fields: [
        { path: ["regime", "benchmark_symbol"], label: "Benchmark symbol", help: "The index whose behaviour defines the market regime for every asset.", type: "text" },
        { path: ["regime", "n_states"], label: "Number of states", help: "How many hidden regimes the model may discover.", type: "int", min: 2, max: 6 },
        {
          path: ["regime", "covariance_type"],
          label: "Covariance type",
          help: "Spherical is the safe default: with few features and limited history, 'full' overfits into duplicate states and can fail to isolate crashes entirely.",
          type: "enum",
          options: ["spherical", "diag", "tied", "full"],
        },
        { path: ["regime", "volatility_window"], label: "Volatility window", help: "Rolling window for the realized-volatility feature.", type: "int", min: 5, max: 120 },
        { path: ["regime", "fast_ma"], label: "Fast MA", help: "Numerator of the trend feature.", type: "int", min: 5, max: 200 },
        { path: ["regime", "slow_ma"], label: "Slow MA", help: "Denominator of the trend feature.", type: "int", min: 20, max: 400 },
        { path: ["regime", "atr_period"], label: "ATR period", help: "Window for the normalized-ATR feature.", type: "int", min: 5, max: 100 },
        {
          path: ["regime", "train_fraction"],
          label: "Training fraction",
          help: "Share of history used to FIT the model. Later dates are inferred with the frozen model, so the classifier never sees its own future.",
          type: "float",
          min: 0.1,
          max: 0.9,
          step: 0.05,
        },
        {
          path: ["regime", "min_regime_persistence_days"],
          label: "Regime persistence (days)",
          help: "A new regime must hold this many days before capital is rerouted, preventing whipsaw on one-day misclassifications.",
          type: "int",
          min: 1,
          max: 30,
        },
        { path: ["regime", "random_seed"], label: "Random seed", help: "Keeps regime assignment reproducible across runs.", type: "int", min: 0, max: 999999 },
      ],
    },
    {
      title: "Risk management",
      description: "Position sizing and the hard gates that cap exposure.",
      fields: [
        {
          path: ["risk", "target_annual_volatility"],
          label: "Target volatility",
          help: "Positions are sized so each asset contributes similar risk: quiet assets get more exposure, wild ones less.",
          type: "float",
          min: 0.01,
          max: 1,
          step: 0.01,
          asPercent: true,
        },
        { path: ["risk", "atr_period"], label: "ATR period", help: "Window used to estimate volatility for sizing.", type: "int", min: 5, max: 100 },
        { path: ["risk", "max_position_leverage"], label: "Max position leverage", help: "Cap on any single asset-strategy exposure.", type: "float", min: 0.1, max: 5, step: 0.1 },
        { path: ["risk", "max_portfolio_heat"], label: "Max portfolio heat", help: "Cap on total simultaneous gross exposure across all positions.", type: "float", min: 0.5, max: 10, step: 0.1 },
        {
          path: ["risk", "drawdown_threshold"],
          label: "Equity-stop drawdown",
          help: "Rolling drawdown that trips the equity-curve stop for a strategy.",
          type: "float",
          min: 0.01,
          max: 1,
          step: 0.01,
          asPercent: true,
        },
        { path: ["risk", "drawdown_window_days"], label: "Drawdown window (days)", help: "Window over which that drawdown is measured.", type: "int", min: 5, max: 250 },
        {
          path: ["risk", "drawdown_scale_factor"],
          label: "Exposure while stopped",
          help: "Multiplier applied while the stop is active. 0 halts the strategy entirely.",
          type: "float",
          min: 0,
          max: 1,
          step: 0.05,
        },
        { path: ["risk", "recovery_days"], label: "Recovery days", help: "Days back below the threshold before trading resumes — prevents the stop chattering on and off.", type: "int", min: 1, max: 120 },
      ],
    },
    {
      title: "Capital routing by regime",
      description:
        "Which strategy families receive capital in each market state, and how much total exposure to run.",
      fields: [
        { path: ["allocation", "regime_strategies", 1], label: "Bull / Trending strategies", help: "Deployed when the market is trending up. Trend-following usually belongs here.", type: "stringList" },
        { path: ["allocation", "regime_strategies", 2], label: "Choppy / Ranging strategies", help: "Deployed in sideways markets. Mean reversion usually belongs here.", type: "stringList" },
        { path: ["allocation", "regime_strategies", 0], label: "Bear / Crash strategies", help: "Deployed defensively during crashes.", type: "stringList" },
        { path: ["allocation", "regime_exposure", 1], label: "Bull exposure multiplier", help: "Overall exposure scaling in a trending regime.", type: "float", min: 0, max: 2, step: 0.05 },
        { path: ["allocation", "regime_exposure", 2], label: "Ranging exposure multiplier", help: "Overall exposure scaling in a choppy regime.", type: "float", min: 0, max: 2, step: 0.05 },
        { path: ["allocation", "regime_exposure", 0], label: "Bear exposure multiplier", help: "Scale down hard in a crash. 0.3 means running 30% of normal size.", type: "float", min: 0, max: 2, step: 0.05 },
        { path: ["allocation", "defensive_assets"], label: "Defensive assets", help: "Capital rotates into these during a bear regime, if they are in the deployed set.", type: "stringList" },
        { path: ["allocation", "correlation_window"], label: "Correlation window", help: "Trailing window used to weight overlapping signals inversely to their correlation.", type: "int", min: 20, max: 500 },
        { path: ["allocation", "min_weight"], label: "Minimum weight", help: "Floor on any single strand's blended weight.", type: "float", min: 0, max: 0.5, step: 0.01 },
      ],
    },
  ],
};

export const CONFIG_FILES: ConfigFile[] = [
  universeConfig,
  strategyGridConfig,
  backtestConfig,
  robustnessConfig,
  regimeConfig,
];

export function getConfigFile(file: string): ConfigFile | undefined {
  return CONFIG_FILES.find((c) => c.file === file);
}

export function findField(file: string, path: (string | number)[]): Field | undefined {
  const config = getConfigFile(file);
  if (!config) return undefined;
  const key = JSON.stringify(path);
  for (const group of config.groups) {
    const match = group.fields.find((f) => JSON.stringify(f.path) === key);
    if (match) return match;
  }
  return undefined;
}

export function configsForLayer(layer: number): ConfigFile[] {
  return CONFIG_FILES.filter((c) => c.layer === layer);
}
