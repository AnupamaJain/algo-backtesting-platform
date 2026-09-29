/* The workspace.
 *
 * Everything here reads the same public API the docs describe. There is no
 * private endpoint behind the sign-in: the account scopes watchlists and
 * saved screens, it does not unlock different numbers.
 *
 * Three rules this file keeps, because they are the same rules the engines
 * keep and a UI that broke them would make the rest of the system a lie:
 *
 *   - Absence renders as "—", never as zero. A missing relative strength is
 *     not a relative strength of nothing.
 *   - Failures are shown. The failed-breakouts engine is in the same menu as
 *     the rest, not hidden behind a toggle nobody finds.
 *   - Every number keeps the provenance the payload gave it. If a scan is
 *     stale, the header says so rather than quietly serving old rows.
 */
(function () {
  "use strict";

  var API = "/api/v1";
  var $ = function (id) { return document.getElementById(id); };
  var state = { engine: "vcp", rows: [], sort: "score", dir: -1, symbol: null };

  function token() {
    try { return localStorage.getItem("vriddhix-token"); } catch (e) { return null; }
  }

  function get(path) {
    var headers = {};
    var t = token();
    if (t) headers.Authorization = "Bearer " + t;
    return fetch(API + path, { headers: headers }).then(function (r) {
      if (!r.ok) throw new Error(r.status + " " + path);
      return r.json();
    });
  }

  /* A value the engine did not measure is not zero. */
  function num(v, digits) {
    if (v === null || v === undefined || v !== v) return "—";
    return Number(v).toFixed(digits === undefined ? 0 : digits);
  }
  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  // Mirrors sector_label() on the server. Without the acronym list, IT
  // renders as "It" and FMCG as "Fmcg", which looks like a bug because it
  // is one.
  var ACRONYMS = { IT:1, FMCG:1, ETF:1, NBFC:1, QSR:1, PSU:1, AMC:1, BFSI:1 };
  function titleish(s) {
    if (!s) return "—";
    return String(s).replace(/_/g, " ").split(/\s+/).map(function (w) {
      if (!w) return w;
      if (ACRONYMS[w.toUpperCase()]) return w.toUpperCase();
      return w.charAt(0).toUpperCase() + w.slice(1).toLowerCase();
    }).join(" ");
  }

  // ---- market -------------------------------------------------------------

  function loadMarket() {
    get("/market").then(function (d) {
      var r = d.regime || {};
      var b = d.breadth || {};
      var cls = "r-" + String(r.regime || "").toLowerCase();
      $("ws-regime").innerHTML =
        '<div class="ws-reg ' + cls + '">' +
          '<b>' + esc(titleish(r.regime)) + '</b>' +
          '<span class="num">' + num(r.score, 1) + '</span>' +
        '</div>' +
        '<dl class="ws-kv">' +
          '<div><dt>confidence</dt><dd class="num">' + num(r.confidence, 0) + '</dd></div>' +
          '<div><dt>advancing</dt><dd class="num">' + num(b.advancers) + '</dd></div>' +
          '<div><dt>declining</dt><dd class="num">' + num(b.decliners) + '</dd></div>' +
          '<div><dt>as of</dt><dd class="num">' + esc(r.date || "—") + '</dd></div>' +
        '</dl>' +
        (r.pending_regime
          ? '<p class="ws-pending">Hysteresis is holding <b>' +
            esc(titleish(r.regime)) + '</b>; the raw reading is <b>' +
            esc(titleish(r.pending_regime)) + '</b>.</p>'
          : "");
    }).catch(function () {
      $("ws-regime").innerHTML = '<span class="faint">Market unavailable.</span>';
    });
  }

  function loadSectors() {
    get("/sectors").then(function (d) {
      var sel = $("ws-sector");
      (d.sectors || []).forEach(function (s) {
        var o = document.createElement("option");
        o.value = s.code || s;
        o.textContent = titleish(s.name || s.code || s);
        sel.appendChild(o);
      });
    }).catch(function () {});
  }

  // ---- scanner ------------------------------------------------------------

  // Each engine returns its own shape. Normalising here rather than in five
  // render paths keeps the table honest about what it does not have.
  function normalise(engine, row) {
    return {
      symbol: row.symbol,
      sector: row.sector || row.sector_code || null,
      stage:  row.vcp_stage || row.stage || row.status || row.event_type || null,
      score:  row.vcp_score !== undefined ? row.vcp_score
            : (row.score !== undefined ? row.score : null),
      rs:     row.rs_score !== undefined ? row.rs_score
            : (row.rs_at_breakout !== undefined ? row.rs_at_breakout : null),
      close:  row.close !== undefined ? row.close : null,
      on:     row.breakout_date || row.detected_on || row.date || null
    };
  }

  function query() {
    var engine = $("ws-engine").value;
    var score = $("ws-score").value;
    var stage = $("ws-stage").value;
    var sector = $("ws-sector").value;

    var qs = ["limit=200"];
    if (engine === "vcp") {
      if (score > 0) qs.push("min_score=" + score);
      if (stage) qs.push("stage=" + stage);
    }
    if (sector) qs.push("sector=" + encodeURIComponent(sector));

    $("ws-rows").innerHTML = '<tr><td colspan="7" class="ws-empty">Loading…</td></tr>';

    get("/scanners/" + engine + "?" + qs.join("&")).then(function (d) {
      var raw = d.results || d.rows || d.breakouts || [];
      state.rows = raw.map(function (r) { return normalise(engine, r); });
      $("ws-n").textContent = d.total !== undefined ? d.total : state.rows.length;
      render();
    }).catch(function (err) {
      state.rows = [];
      $("ws-n").textContent = "—";
      $("ws-rows").innerHTML =
        '<tr><td colspan="7" class="ws-empty">Could not load: ' +
        esc(err.message) + '</td></tr>';
    });
  }

  function render() {
    var rows = state.rows.slice().sort(function (a, b) {
      var x = a[state.sort], y = b[state.sort];
      if (x === null || x === undefined) return 1;   // unmeasured sinks, either direction
      if (y === null || y === undefined) return -1;
      if (typeof x === "string") return x.localeCompare(y) * state.dir;
      return (x - y) * state.dir;
    });

    if (!rows.length) {
      $("ws-rows").innerHTML =
        '<tr><td colspan="7" class="ws-empty">Nothing matches. That is a ' +
        'result, not an error — widen the filters.</td></tr>';
      return;
    }

    $("ws-rows").innerHTML = rows.map(function (r) {
      var stage = r.stage ? '<span class="stage stage-' + esc(r.stage) + '">' +
                            esc(titleish(r.stage)) + '</span>' : "—";
      return '<tr data-symbol="' + esc(r.symbol) + '"' +
             (r.symbol === state.symbol ? ' class="on"' : '') + '>' +
        '<td><b>' + esc(r.symbol) + '</b></td>' +
        '<td class="faint">' + esc(titleish(r.sector)) + '</td>' +
        '<td>' + stage + '</td>' +
        '<td class="r num">' + num(r.score) + '</td>' +
        '<td class="r num">' + num(r.rs) + '</td>' +
        '<td class="r num">' + num(r.close, 2) + '</td>' +
        '<td class="r"><button class="ws-add" data-add="' + esc(r.symbol) +
          '" title="Add to watchlist">+</button></td>' +
      '</tr>';
    }).join("");
  }

  // ---- detail -------------------------------------------------------------

  function spark(candles) {
    if (!candles || candles.length < 2) return "";
    var closes = candles.map(function (c) { return c.close; });
    var lo = Math.min.apply(null, closes), hi = Math.max.apply(null, closes);
    var span = (hi - lo) || 1;
    var pts = closes.map(function (c, i) {
      return (i / (closes.length - 1) * 100).toFixed(2) + "," +
             (38 - (c - lo) / span * 36).toFixed(2);
    }).join(" ");
    var up = closes[closes.length - 1] >= closes[0];
    return '<svg class="ws-spark" viewBox="0 0 100 40" preserveAspectRatio="none">' +
           '<polyline points="' + pts + '" fill="none" stroke-width="1.4" ' +
           'vector-effect="non-scaling-stroke" stroke="' +
           (up ? "var(--bull)" : "var(--bear)") + '"/></svg>';
  }

  function components(list) {
    if (!list || !list.length) return "";
    return '<div class="ws-comp">' + list.map(function (c) {
      return '<div class="ws-comp-row">' +
        '<span>' + esc(titleish(c.name)) + '</span>' +
        '<span class="ws-bar"><i style="width:' +
          Math.max(0, Math.min(100, c.raw)).toFixed(1) + '%"></i></span>' +
        '<span class="num">' + num(c.raw) + '</span>' +
        '<span class="faint num">×' + num(c.weight, 2) + '</span>' +
      '</div>';
    }).join("") + '</div>';
  }

  function openSymbol(symbol) {
    state.symbol = symbol;
    render();
    var el = $("ws-detail");
    el.innerHTML = '<div class="ws-detail-empty"><p>Loading ' + esc(symbol) + '…</p></div>';

    Promise.all([
      get("/stocks/" + symbol + "/xray").catch(function () { return null; }),
      get("/stocks/" + symbol + "/chart?days=180").catch(function () { return null; }),
      get("/breakouts?symbol=" + symbol + "&limit=6").catch(function () { return null; })
    ]).then(function (res) {
      var x = res[0] || {}, chart = res[1] || {}, bo = res[2] || {};
      var pats = x.patterns || [];
      var top = pats[0] || null;
      var sector = (state.rows.filter(function (r) {
        return r.symbol === symbol; })[0] || {}).sector;

      var breakouts = (bo.breakouts || []).slice(0, 6).map(function (b) {
        // Three states, not two. PENDING has not resolved, and painting it
        // green would claim an outcome the ledger has not recorded.
        var status = String(b.status).toUpperCase();
        var tone = status === "FAILED" ? "pill-bad"
                 : status === "CONFIRMED" ? "pill-ok" : "pill-wait";
        var o = b.outcome || {};
        return '<tr><td class="num">' + esc(b.breakout_date || "—") + '</td>' +
          '<td><span class="pill ' + tone + '">' +
          esc(titleish(b.status)) + '</span></td>' +
          '<td class="r num">' + num(o.mfe_pct, 1) + '%</td>' +
          '<td class="r num">' + num(o.mae_pct, 1) + '%</td></tr>';
      }).join("");

      el.innerHTML =
        '<header class="ws-dh">' +
          '<div><h2>' + esc(symbol) + '</h2>' +
          '<span class="faint">' + esc(titleish(sector)) + '</span></div>' +
          '<a class="ws-api" href="/api/v1/stocks/' + esc(symbol) +
            '/xray" target="_blank" rel="noopener">raw ↗</a>' +
        '</header>' +
        (x.is_stale
          ? '<p class="ws-stale">Last scan ' + esc(x.latest_scan_date || "—") +
            ', ' + num(x.age_hours, 0) + 'h old.</p>'
          : "") +
        spark(chart.candles) +
        (top
          ? '<div class="ws-sec"><div class="ws-h">' +
              esc(top.type || "VCP") + ' · ' + esc(titleish(top.status)) +
              ' <b class="num">' + num(top.score, 1) + '</b></div>' +
              components(top.score_components) +
            '</div>'
          : '<div class="ws-sec"><p class="faint">No pattern recorded on this name.</p></div>') +
        (breakouts
          ? '<div class="ws-sec"><div class="ws-h">Breakouts, failures kept</div>' +
            '<table class="ws-mini-t"><thead><tr><th>Date</th><th>Outcome</th>' +
            '<th class="r">Best</th><th class="r">Worst</th></tr></thead>' +
            '<tbody>' + breakouts + '</tbody></table></div>'
          : '<div class="ws-sec"><p class="faint">No breakout has been recorded ' +
            'on this name yet.</p></div>');
    });
  }

  // ---- watchlist ----------------------------------------------------------

  function loadWatchlist() {
    if (!token()) return;
    get("/watchlists").then(function (d) {
      var lists = d.watchlists || [];
      if (!lists.length) {
        $("ws-wl").innerHTML = '<span class="faint">No list yet.</span>';
        return;
      }
      state.wl = lists[0];
      var items = (lists[0].items || []);
      $("ws-wl").innerHTML = items.length
        ? items.map(function (i) {
            return '<button class="ws-chip" data-symbol="' + esc(i.symbol) + '">' +
                   esc(i.symbol) + '</button>';
          }).join("")
        : '<span class="faint">Empty. Use + on any row.</span>';
    }).catch(function () {});
  }

  function addToWatchlist(symbol) {
    if (!token()) { location.href = "/login"; return; }
    var go = state.wl
      ? Promise.resolve(state.wl)
      : fetch(API + "/watchlists", {
          method: "POST",
          headers: { "Content-Type": "application/json",
                     Authorization: "Bearer " + token() },
          body: JSON.stringify({ name: "Watchlist" })
        }).then(function (r) { return r.json(); });

    go.then(function (wl) {
      state.wl = wl;
      return fetch(API + "/watchlists/" + (wl.id || wl.watchlist_id) + "/items", {
        method: "POST",
        headers: { "Content-Type": "application/json",
                   Authorization: "Bearer " + token() },
        body: JSON.stringify({ symbol: symbol })
      });
    }).then(loadWatchlist).catch(function () {});
  }

  // ---- wiring -------------------------------------------------------------

  function debounce(fn, ms) {
    var t; return function () { clearTimeout(t); t = setTimeout(fn, ms); };
  }
  var requery = debounce(query, 220);

  $("ws-engine").addEventListener("change", function () {
    // Only the VCP scanner scores and stages its rows; leaving the controls
    // visible for the others would imply a filter that does nothing.
    var isVcp = $("ws-engine").value === "vcp";
    $("ws-score-wrap").hidden = !isVcp;
    $("ws-stage-wrap").hidden = !isVcp;
    query();
  });
  $("ws-score").addEventListener("input", function () {
    $("ws-score-v").textContent = this.value;
    requery();
  });
  $("ws-stage").addEventListener("change", query);
  $("ws-sector").addEventListener("change", query);

  document.querySelectorAll(".ws-table th[data-sort]").forEach(function (th) {
    th.addEventListener("click", function () {
      var key = th.dataset.sort;
      state.dir = state.sort === key ? -state.dir : -1;
      state.sort = key;
      render();
    });
  });

  $("ws-rows").addEventListener("click", function (e) {
    var add = e.target.closest("[data-add]");
    if (add) { e.stopPropagation(); addToWatchlist(add.dataset.add); return; }
    var tr = e.target.closest("tr[data-symbol]");
    if (tr) openSymbol(tr.dataset.symbol);
  });

  $("ws-wl").addEventListener("click", function (e) {
    var chip = e.target.closest("[data-symbol]");
    if (chip) openSymbol(chip.dataset.symbol);
  });

  loadMarket();
  loadSectors();
  loadWatchlist();
  query();
})();
