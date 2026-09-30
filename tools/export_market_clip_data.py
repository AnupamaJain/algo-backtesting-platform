import sys, json, pathlib
sys.path.insert(0, "src")
from vriddhix.db.base import session_scope
import sqlalchemy as sa

out = pathlib.Path(sys.argv[1])

with session_scope() as s:
    # 1. Regime and breadth, every session the engine has scored.
    regime = s.execute(sa.text("""
        SELECT r.date, r.regime, r.regime_score,
               m.index_close, m.pct_above_ema_50, m.ad_ratio
        FROM market_regimes r
        LEFT JOIN market_metrics m ON m.date = r.date
        ORDER BY r.date
    """)).mappings().all()

    # 2. Sector rotation: relative strength against momentum, by month, so
    #    a clip is 90-odd frames rather than 2,000.
    rotation = s.execute(sa.text("""
        SELECT sm.date, sec.name AS sector, sm.rs_score, sm.momentum_score, sm.quadrant
        FROM sector_metrics sm
        JOIN sectors sec ON sec.id = sm.sector_id
        WHERE sm.date IN (SELECT DISTINCT date FROM sector_metrics
                          WHERE date LIKE '%-01' OR date LIKE '%-15')
          AND sm.low_sample = 0
        ORDER BY sm.date
    """)).mappings().all()

    # 3. Every resolved breakout, in the order it happened.
    ledger = s.execute(sa.text("""
        SELECT b.breakout_date AS date, b.regime_at_breakout AS regime,
               o.failed, o.return_pct, o.days_held
        FROM breakouts b
        JOIN breakout_outcomes o ON o.breakout_id = b.id
        WHERE o.failed IS NOT NULL
        ORDER BY b.breakout_date
    """)).mappings().all()

data = {
    "regime":   [dict(r) for r in regime],
    "rotation": [dict(r) for r in rotation],
    "ledger":   [dict(r) for r in ledger],
}
for key, rows in data.items():
    print(f"  {key:9s} {len(rows):,} rows"
          + (f"  {rows[0]['date']} -> {rows[-1]['date']}" if rows else ""))
(out / "market.json").write_text(json.dumps(data, default=str))
print("  written:", (out / "market.json").stat().st_size // 1024, "KB")

# Run from vriddhix/ with the venv:
#
#     ../venv/bin/python3 ../tools/export_market_clip_data.py <out-dir>
#
# then serve that directory next to tools/clip_studio.html and record
# ?clip=regime|rotation|ledger. The clips on the landing page were made
# this way; re-running it after a fresh scan regenerates them from
# whatever the database holds then.
