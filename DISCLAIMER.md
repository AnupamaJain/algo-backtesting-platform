# Disclaimer

**READ THIS BEFORE USING THE SOFTWARE.**

## Educational Purpose

This project is published **for educational purposes** — to show one way of
building a self-hosted algorithmic trading setup on top of broker APIs —
Dhan for orders and Flattrade for market data. It is a reference implementation, not a packaged product or a
trading signal service.

## Financial Risk

This software places **real orders with real money** on Indian stock
exchanges (NSE/BSE) through the Dhan API when live trading is enabled. Algorithmic trading of futures and options carries a **substantial
risk of loss**. You can lose more than your initial investment.

- Past performance of any strategy in this repository is **not** indicative
  of future results.
- The strategies shipped here were built for the author's personal use and
  risk profile. They are **not investment advice** and are not tuned for you.
- Bugs, network failures, exchange outages, API changes, and market
  conditions can all cause orders to be placed, modified, cancelled, or
  missed in ways you do not expect.

**Please understand the code before using it.** Do not deploy anything you
have not personally reviewed. **You are solely responsible for every order
this software places on your account and for any resulting financial
loss** — the authors and contributors bear none.

## No Warranty

This software is provided **"AS IS"**, without warranty of any kind, express
or implied. The authors and contributors accept **no liability** for any
damages or losses — financial or otherwise — arising from its use. See the
[LICENSE](LICENSE) for the full warranty disclaimer.

## Not Affiliated with Any Broker

This is an independent open-source project. It is **not** affiliated with,
endorsed by, or supported by Dhan, Flattrade, or any exchange. "Dhan" and
"Flattrade" are trademarks of their respective owners. Use of their APIs is
subject to each broker's own terms of service.

## Regulatory Notice

Automated/algorithmic trading may be subject to regulation by SEBI and your
broker's terms. It is **your responsibility** to ensure your use of this
software complies with all laws, regulations, and broker agreements that
apply to you.

## Safe Default

A fresh installation starts in **dry-run mode** (`[safety] live_trading =
false` in `configfile.ini`): every order is simulated and logged, nothing is
sent to the broker. Enable live trading only after you have read the code,
tested your configuration, and accepted the risks above.
