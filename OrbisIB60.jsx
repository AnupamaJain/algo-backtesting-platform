import React, { useState, useEffect, useRef, useMemo } from "react";

/* ============================================================================
   ORBIS — IB-60 Retracement Engine · Interactive Prototype
   Dark terminal aesthetic: base #05080F · neon-green accent · Syne + IBM Plex Mono
   Single-file. Simulated tick engine drives the real RFC state machine.
   ========================================================================== */

const C = {
  base: "#05080F", panel: "#0A0F1A", panel2: "#0D131F", line: "#1A2233",
  ink: "#E6EDF5", dim: "#7A8699", faint: "#4A5468",
  accent: "#00FF9C", accentDim: "#0A7F52",
  buy: "#00FF9C", sell: "#FF5C6C", warn: "#FFC857", cool: "#4DA6FF",
  ibHi: "#FFC857", ibLo: "#4DA6FF", mid: "#00FF9C",
};
const MONO = "'IBM Plex Mono', ui-monospace, monospace";
const DISP = "'Syne', system-ui, sans-serif";

/* ---------- deterministic-ish PRNG so a "day" replays the same ----------- */
function mulberry(seed) {
  return function () {
    seed |= 0; seed = (seed + 0x6D2B79F5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/* ---------- the state machine (mirrors RFC §4) --------------------------- */
const PHASES = ["BUILDING_IB","IB_READY","BLOCKED","BREAK_CONFIRMED","ARMED","IN_TRADE","CLOSED","INVALIDATED"];

function freshDay() {
  return {
    phase: "BUILDING_IB",
    ib: null, firstSideFormed: null, firstBreak: null,
    armed: null, position: null, closed: null,
    bars: [], price: null, t: 0, history: [],
  };
}

/* Simulate one symbol's session. cfg drives mode/vix gate/etc. */
function makeSession(symbol, seed, cfg) {
  const rnd = mulberry(seed);
  const start = 22500 + rnd() * 1500;              // synthetic index level
  const ibVol = 18 + rnd() * 34;                   // IB range size
  const vix = +(11 + rnd() * 12).toFixed(1);       // simulated India VIX
  // decide the "truth" of the day: which side forms first, which breaks first
  const highFirst = rnd() > 0.5;
  // edge: if high forms first, low breaks first ~69%
  const lowBreaksFirst = highFirst ? rnd() < 0.6875 : rnd() < 0.40;
  return { symbol, seed, start, ibVol, vix, highFirst, lowBreaksFirst, rnd };
}

/* advance the day by one "tick"; returns new state (pure-ish) */
function step(state, sess, cfg) {
  const s = { ...state, history: [...state.history] };
  const t = s.t + 1;
  s.t = t;
  const log = (to, reason, px) => s.history.push({ t, to, reason, px: px!=null?+px.toFixed(1):null });

  // IB construction: first 12 ticks = the 60min window (5 ticks/15m feel)
  const IB_TICKS = 12;
  if (t === 1) { s.price = sess.start; s.ibHi = sess.start; s.ibLo = sess.start; log("BUILDING_IB","session open T0", sess.start); }

  // random walk
  const drift = (sess.rnd() - 0.5);
  const vol = s.phase === "BUILDING_IB" ? sess.ibVol/8 : sess.ibVol/6;
  s.price = s.price + drift * vol;

  if (t <= IB_TICKS) {
    // forming IB
    s.ibHi = Math.max(s.ibHi, s.price);
    s.ibLo = Math.min(s.ibLo, s.price);
    // bias to realize the "first side formed" truth midway
    if (t === 6) {
      if (sess.highFirst) s.ibHi = Math.max(s.ibHi, sess.start + sess.ibVol*0.55);
      else s.ibLo = Math.min(s.ibLo, sess.start - sess.ibVol*0.55);
    }
    if (t === IB_TICKS) {
      const H = s.ibHi, L = s.ibLo, R = H - L;
      s.ib = { H, L, R, M: L + 0.5*R, Q25: L + 0.25*R, Q75: L + 0.75*R };
      s.firstSideFormed = sess.highFirst ? "HIGH" : "LOW";
      // gate
      const vixHigh = sess.vix >= cfg.vixMax;
      if (vixHigh) { s.phase = "BLOCKED"; s.blockReason = `VIX ${sess.vix} ≥ ${cfg.vixMax}`; log("BLOCKED", s.blockReason); }
      else { s.phase = "IB_READY"; log("IB_READY", `IB built R=${R.toFixed(1)}`); }
    }
    return s;
  }

  if (s.phase === "BLOCKED" || s.phase === "CLOSED" || s.phase === "INVALIDATED") return s;
  const { H, L, M, Q25, Q75 } = s.ib;

  // detect first break
  if (s.phase === "IB_READY") {
    // push price toward the side that breaks first
    const target = sess.lowBreaksFirst ? L - sess.ibVol*0.15 : H + sess.ibVol*0.15;
    s.price += (target - s.price) * 0.18;
    if (s.price > H) { s.firstBreak = "HIGH"; s.phase = "BREAK_CONFIRMED"; log("BREAK_CONFIRMED","break IB high", s.price); }
    else if (s.price < L) { s.firstBreak = "LOW"; s.phase = "BREAK_CONFIRMED"; log("BREAK_CONFIRMED","break IB low", s.price); }
    return s;
  }

  // arm at the level
  if (s.phase === "BREAK_CONFIRMED") {
    let side, entry, stop, tgt;
    const buf = sess.ibVol * 0.08;
    if (cfg.mode === "REVERSION") {
      if (s.firstBreak === "LOW") { side="SELL"; entry=M; stop=L-buf; tgt=H; }
      else { side="BUY"; entry=M; stop=H+buf; tgt=L; }
    } else { // CONTINUATION
      if (s.firstBreak === "HIGH") { side="BUY"; entry=Q25; stop=M; tgt=H + s.ib.R*0.5; }
      else { side="SELL"; entry=Q75; stop=M; tgt=L - s.ib.R*0.5; }
    }
    const rr = Math.abs(tgt-entry)/Math.abs(entry-stop);
    s.armed = { side, entry, stop, tgt, rr };
    s.phase = "ARMED";
    log("ARMED", `${side} @ ${entry.toFixed(1)} · RR ${rr.toFixed(2)}`, entry);
    return s;
  }

  // armed: pull price back toward entry; fill on touch
  if (s.phase === "ARMED") {
    const a = s.armed;
    s.price += (a.entry - s.price) * 0.16 + (sess.rnd()-0.5)*sess.ibVol/9;
    // invalidation: opposite extreme (double break) before fill
    if ((s.firstBreak==="LOW" && s.price > H) || (s.firstBreak==="HIGH" && s.price < L)) {
      s.phase="INVALIDATED"; log("INVALIDATED","double break before fill", s.price); return s;
    }
    if (t > 34) { s.phase="INVALIDATED"; log("INVALIDATED","entry cutoff", s.price); return s; }
    const touched = Math.abs(s.price - a.entry) < sess.ibVol*0.06;
    if (touched) {
      s.position = { ...a, fill: a.entry };
      s.phase = "IN_TRADE"; log("IN_TRADE", `filled ${a.side} @ ${a.entry.toFixed(1)}`, a.entry);
    }
    return s;
  }

  // in trade: walk to target or stop
  if (s.phase === "IN_TRADE") {
    const p = s.position;
    const toward = p.side==="SELL" ? -1 : 1;
    // bias toward target with noise
    s.price += toward * sess.ibVol/10 * (0.4 + sess.rnd()*0.5) + (sess.rnd()-0.5)*sess.ibVol/8;
    const hitTgt = p.side==="SELL" ? s.price <= p.tgt : s.price >= p.tgt;
    const hitStop = p.side==="SELL" ? s.price >= p.stop : s.price <= p.stop;
    if (hitStop) { close(s,"SL",p.stop,p,log); return s; }
    if (hitTgt) { close(s,"TP",p.tgt,p,log); return s; }
    if (t > 48) { close(s,"TIME",s.price,p,log); return s; }
    return s;
  }
  return s;
}
function close(s, reason, px, p, log) {
  const pts = (p.side==="SELL" ? p.fill - px : px - p.fill);
  const r = pts / Math.abs(p.fill - p.stop);
  s.closed = { reason, px, pts:+pts.toFixed(1), r:+r.toFixed(2), side:p.side };
  s.position = null; s.phase = "CLOSED";
  log("CLOSED", `${reason} ${pts>=0?"+":""}${pts.toFixed(1)} pts (${r.toFixed(2)}R)`, px);
}

/* ====================== shared atoms ===================================== */
const Mono = ({ children, c=C.ink, s=13, b=false, style }) =>
  <span style={{ fontFamily:MONO, color:c, fontSize:s, fontWeight:b?600:400, ...style }}>{children}</span>;

function PhaseTag({ phase }) {
  const map = {
    BUILDING_IB:[C.dim,"building"], IB_READY:[C.cool,"ready"], BLOCKED:[C.sell,"blocked"],
    BREAK_CONFIRMED:[C.warn,"break"], ARMED:[C.accent,"armed"], IN_TRADE:[C.accent,"in trade"],
    CLOSED:[C.ink,"closed"], INVALIDATED:[C.faint,"void"],
  };
  const [col,label] = map[phase] || [C.dim,phase];
  return <span style={{ fontFamily:MONO, fontSize:10, letterSpacing:1, textTransform:"uppercase",
    color:col, border:`1px solid ${col}44`, background:`${col}11`, padding:"2px 7px", borderRadius:3,
    boxShadow: phase==="ARMED"||phase==="IN_TRADE" ? `0 0 12px ${col}55` : "none" }}>{label}</span>;
}

/* The signature element: a live vertical IB ladder */
function IBLadder({ day, h=210, w=128, compact=false }) {
  if (!day.ib) return (
    <div style={{ height:h, width:w, display:"flex", alignItems:"center", justifyContent:"center",
      border:`1px dashed ${C.line}`, borderRadius:6 }}>
      <Mono c={C.faint} s={10}>building IB…</Mono>
    </div>
  );
  const { H, L } = day.ib;
  const pad = (H-L)*0.45;
  const top = H+pad, bot = L-pad, span = top-bot;
  const y = v => ((top - v)/span)*h;
  const rows = [
    { v:H, lab:"HIGH", c:C.ibHi },
    { v:day.ib.Q75, lab:"75", c:C.faint },
    { v:day.ib.M, lab:"50", c:C.mid },
    { v:day.ib.Q25, lab:"25", c:C.faint },
    { v:L, lab:"LOW", c:C.ibLo },
  ];
  const a = day.armed || day.position;
  return (
    <svg width={w} height={h} style={{ overflow:"visible" }}>
      {/* IB box */}
      <rect x={26} y={y(H)} width={w-52} height={y(L)-y(H)} fill={`${C.accent}07`} stroke={`${C.line}`} />
      {rows.map((r,i)=>(
        <g key={i}>
          <line x1={26} y1={y(r.v)} x2={w-26} y2={y(r.v)}
            stroke={r.c} strokeWidth={r.lab==="50"?1.4:1} strokeDasharray={r.lab==="50"?"none":"3 3"} opacity={0.8}/>
          <text x={0} y={y(r.v)+3} fill={r.c} fontFamily={MONO} fontSize={9}>{r.lab}</text>
          {!compact && <text x={w-22} y={y(r.v)+3} fill={C.faint} fontFamily={MONO} fontSize={9}>{r.v.toFixed(0)}</text>}
        </g>
      ))}
      {/* entry / stop / target overlays */}
      {a && <>
        <line x1={26} y1={y(a.entry)} x2={w-26} y2={y(a.entry)} stroke={C.accent} strokeWidth={1} opacity={0.5}/>
        <line x1={26} y1={y(a.stop)} x2={w-26} y2={y(a.stop)} stroke={C.sell} strokeWidth={1} strokeDasharray="2 2" opacity={0.6}/>
        <line x1={26} y1={y(a.tgt)} x2={w-26} y2={y(a.tgt)} stroke={C.buy} strokeWidth={1} strokeDasharray="2 2" opacity={0.6}/>
      </>}
      {/* live price marker */}
      {day.price!=null && (() => {
        const py = Math.max(2, Math.min(h-2, y(day.price)));
        return <g>
          <polygon points={`${w-26},${py} ${w-20},${py-4} ${w-20},${py+4}`} fill={C.ink}/>
          <line x1={26} y1={py} x2={w-26} y2={py} stroke={C.ink} strokeWidth={0.6} opacity={0.35}/>
        </g>;
      })()}
    </svg>
  );
}

/* ====================== top-level app ==================================== */
const SYMBOLS = ["NIFTY","BANKNIFTY","FINNIFTY","MIDCPNIFTY"];
const TABS = ["Dashboard","Scanner","Strategy Builder","Stat Reports","Signals"];

export default function App() {
  const [tab, setTab] = useState("Dashboard");
  const [cfg, setCfg] = useState({
    mode:"REVERSION", breakMeasure:"WICK", entryLevel:"MID",
    fillConfirm:"TOUCH", stopPolicy:"STRUCTURAL", rrTarget:1.0, vixMax:18,
    weekday:"Tuesday", conditionFirstSide:"ANY",
  });
  const [running, setRunning] = useState(true);
  const [speed, setSpeed] = useState(1);
  const [seedBase, setSeedBase] = useState(7);

  // one session + state per symbol
  const sessions = useMemo(()=> SYMBOLS.map((sym,i)=> makeSession(sym, seedBase*97 + i*13, cfg)),
    [seedBase, cfg.vixMax]);
  const [states, setStates] = useState(()=> SYMBOLS.map(freshDay));

  // tick loop
  useEffect(()=>{
    if(!running) return;
    const id = setInterval(()=>{
      setStates(prev => prev.map((st,i)=> step(st, sessions[i], cfg)));
    }, 520/speed);
    return ()=>clearInterval(id);
  },[running,speed,sessions,cfg]);

  const reset = (newSeed) => {
    setSeedBase(newSeed ?? seedBase+1);
    setStates(SYMBOLS.map(freshDay));
    setRunning(true);
  };

  return (
    <div style={{ background:C.base, minHeight:"100vh", color:C.ink, fontFamily:DISP,
      backgroundImage:`radial-gradient(circle at 80% -10%, ${C.accent}0A, transparent 45%)` }}>
      <style>{`@import url('https://fonts.googleapis.com/css2?family=Syne:wght@600;700;800&family=IBM+Plex+Mono:wght@400;500;600&display=swap');
        *{box-sizing:border-box} ::selection{background:${C.accent}33}
        .tabbtn:hover{color:${C.ink}!important}
        .row:hover{background:${C.panel2}!important}
        @keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}`}</style>

      {/* header */}
      <header style={{ display:"flex", alignItems:"center", gap:18, padding:"14px 22px",
        borderBottom:`1px solid ${C.line}`, position:"sticky", top:0, zIndex:10,
        background:`${C.base}EE`, backdropFilter:"blur(8px)" }}>
        <div style={{ display:"flex", alignItems:"center", gap:10 }}>
          <div style={{ width:9, height:9, borderRadius:2, background:C.accent, boxShadow:`0 0 10px ${C.accent}` }}/>
          <span style={{ fontFamily:DISP, fontWeight:800, fontSize:20, letterSpacing:-0.5 }}>ORBIS</span>
          <Mono c={C.dim} s={11} style={{ marginLeft:4 }}>IB-60 ENGINE</Mono>
        </div>
        <nav style={{ display:"flex", gap:4, marginLeft:14 }}>
          {TABS.map(t=>(
            <button key={t} className="tabbtn" onClick={()=>setTab(t)}
              style={{ background:tab===t?C.panel:"transparent", color:tab===t?C.accent:C.dim,
                border:`1px solid ${tab===t?C.line:"transparent"}`, borderRadius:6, padding:"7px 13px",
                fontFamily:MONO, fontSize:12, cursor:"pointer", transition:"all .15s" }}>{t}</button>
          ))}
        </nav>
        <div style={{ marginLeft:"auto", display:"flex", alignItems:"center", gap:8 }}>
          <button onClick={()=>setRunning(r=>!r)} style={btn(running?C.warn:C.accent)}>{running?"❚❚ pause":"▶ run"}</button>
          <button onClick={()=>setSpeed(s=> s>=4?1:s*2)} style={btn(C.dim)}>{speed}×</button>
          <button onClick={()=>reset()} style={btn(C.cool)}>↻ new day</button>
        </div>
      </header>

      <main style={{ padding:22, maxWidth:1180, margin:"0 auto" }}>
        {tab==="Dashboard" && <Dashboard states={states} sessions={sessions} cfg={cfg}/>}
        {tab==="Scanner" && <Scanner states={states} sessions={sessions} cfg={cfg} onOpen={()=>setTab("Signals")}/>}
        {tab==="Strategy Builder" && <Builder cfg={cfg} setCfg={setCfg}/>}
        {tab==="Stat Reports" && <StatReports/>}
        {tab==="Signals" && <Signals states={states} sessions={sessions} cfg={cfg}/>}
      </main>
    </div>
  );
}
const btn = (c) => ({ background:`${c}14`, color:c, border:`1px solid ${c}55`, borderRadius:6,
  padding:"7px 12px", fontFamily:MONO, fontSize:12, cursor:"pointer" });

/* ---------------------------- DASHBOARD ---------------------------------- */
function Dashboard({ states, sessions, cfg }) {
  const blocked = sessions.filter(s=> s.vix>=cfg.vixMax).length;
  const armed = states.filter(s=> s.phase==="ARMED"||s.phase==="IN_TRADE").length;
  const closed = states.filter(s=> s.closed);
  const pnl = closed.reduce((a,s)=> a + (s.closed?.pts||0), 0);
  // representative VIX = max across symbols (worst gate)
  const vix = Math.max(...sessions.map(s=>s.vix));
  const tradable = vix < cfg.vixMax;

  return (
    <div>
      <div style={{ display:"flex", gap:14, marginBottom:18 }}>
        {/* VIX GATE — loud, per PRD */}
        <div style={{ flex:"0 0 300px", background:tradable?`${C.accent}0C`:`${C.sell}10`,
          border:`1px solid ${tradable?C.accent:C.sell}55`, borderRadius:10, padding:18 }}>
          <Mono c={C.dim} s={10} style={{ letterSpacing:2 }}>INDIA VIX · GATE @ T0+60</Mono>
          <div style={{ display:"flex", alignItems:"baseline", gap:10, marginTop:6 }}>
            <span style={{ fontFamily:DISP, fontWeight:800, fontSize:46, color:tradable?C.accent:C.sell, lineHeight:1 }}>{vix.toFixed(1)}</span>
            <Mono c={C.dim} s={12}>/ max {cfg.vixMax}</Mono>
          </div>
          <div style={{ marginTop:12, fontFamily:MONO, fontSize:13, fontWeight:600,
            color:tradable?C.accent:C.sell }}>
            {tradable ? "✓ TRADABLE — setups armed" : "✕ BLOCKED — not a trading day"}
          </div>
          {!tradable && <Mono c={C.dim} s={11} style={{ display:"block", marginTop:6 }}>
            VIX elevated. No entries today. Preserve capital.</Mono>}
        </div>

        {[["armed/live", armed, C.accent],["closed today", closed.length, C.cool],
          ["net points", (pnl>=0?"+":"")+pnl.toFixed(1), pnl>=0?C.buy:C.sell],
          ["blocked", `${blocked}/${SYMBOLS.length}`, C.warn]].map(([l,v,c],i)=>(
          <div key={i} style={{ flex:1, background:C.panel, border:`1px solid ${C.line}`, borderRadius:10, padding:18 }}>
            <Mono c={C.dim} s={10} style={{ letterSpacing:2 }}>{l.toUpperCase()}</Mono>
            <div style={{ fontFamily:DISP, fontWeight:800, fontSize:34, color:c, marginTop:8 }}>{v}</div>
          </div>
        ))}
      </div>

      <Mono c={C.dim} s={11} style={{ letterSpacing:2, display:"block", marginBottom:10 }}>WHAT'S IN PLAY · {cfg.mode}</Mono>
      <div style={{ display:"grid", gridTemplateColumns:"repeat(auto-fill,minmax(255px,1fr))", gap:14 }}>
        {states.map((st,i)=>(
          <div key={i} style={{ background:C.panel, border:`1px solid ${st.phase==="ARMED"||st.phase==="IN_TRADE"?C.accent+"66":C.line}`,
            borderRadius:10, padding:14, display:"flex", gap:12,
            boxShadow: st.phase==="ARMED"||st.phase==="IN_TRADE" ? `0 0 22px ${C.accent}15` : "none" }}>
            <IBLadder day={st} h={150} w={108} compact/>
            <div style={{ flex:1, minWidth:0 }}>
              <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center" }}>
                <span style={{ fontFamily:DISP, fontWeight:700, fontSize:15 }}>{SYMBOLS[i]}</span>
                <PhaseTag phase={st.phase}/>
              </div>
              <div style={{ marginTop:8, display:"grid", gap:3 }}>
                <KV k="VIX" v={sessions[i].vix} c={sessions[i].vix>=cfg.vixMax?C.sell:C.dim}/>
                <KV k="formed 1st" v={st.firstSideFormed||"—"}/>
                <KV k="broke 1st" v={st.firstBreak||"—"} c={st.firstBreak?C.warn:C.dim}/>
                {st.armed && <KV k={st.armed.side} v={st.armed.entry.toFixed(0)} c={st.armed.side==="SELL"?C.sell:C.buy}/>}
                {st.closed && <KV k="result" v={`${st.closed.pts>=0?"+":""}${st.closed.pts} (${st.closed.r}R)`}
                  c={st.closed.pts>=0?C.buy:C.sell}/>}
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
const KV = ({k,v,c=C.ink}) => (
  <div style={{ display:"flex", justifyContent:"space-between" }}>
    <Mono c={C.faint} s={11}>{k}</Mono><Mono c={c} s={11} b>{v}</Mono>
  </div>
);

/* ----------------------------- SCANNER ----------------------------------- */
function Scanner({ states, sessions, cfg, onOpen }) {
  const cols = ["symbol","IB high","IB low","range","mid","formed 1st","broke 1st","state","VIX","signal"];
  const order = { ARMED:0, IN_TRADE:0, BREAK_CONFIRMED:1, IB_READY:2, BUILDING_IB:3, CLOSED:4, INVALIDATED:5, BLOCKED:6 };
  const rows = states.map((st,i)=>({st,i})).sort((a,b)=> (order[a.st.phase]??9)-(order[b.st.phase]??9));
  return (
    <div style={{ background:C.panel, border:`1px solid ${C.line}`, borderRadius:10, overflow:"hidden" }}>
      <div style={{ display:"grid", gridTemplateColumns:"1.1fr .9fr .9fr .7fr .9fr 1fr 1fr 1.1fr .6fr 1.4fr",
        padding:"11px 14px", borderBottom:`1px solid ${C.line}`, background:C.panel2 }}>
        {cols.map(c=> <Mono key={c} c={C.dim} s={10} style={{ letterSpacing:1 }}>{c.toUpperCase()}</Mono>)}
      </div>
      {rows.map(({st,i})=>{
        const sig = st.armed ? `${st.armed.side} @ ${st.armed.entry.toFixed(0)} → ${st.armed.tgt.toFixed(0)}`
          : st.closed ? `${st.closed.reason} ${st.closed.pts>=0?"+":""}${st.closed.pts}` : "—";
        const sigC = st.armed ? (st.armed.side==="SELL"?C.sell:C.buy) : st.closed ? (st.closed.pts>=0?C.buy:C.sell) : C.faint;
        return (
          <div key={i} className="row" onClick={onOpen} style={{ display:"grid", cursor:"pointer",
            gridTemplateColumns:"1.1fr .9fr .9fr .7fr .9fr 1fr 1fr 1.1fr .6fr 1.4fr",
            padding:"12px 14px", borderBottom:`1px solid ${C.line}55`, alignItems:"center" }}>
            <span style={{ fontFamily:DISP, fontWeight:700, fontSize:14 }}>{SYMBOLS[i]}</span>
            <Mono c={C.ibHi} s={12}>{st.ib?st.ib.H.toFixed(0):"—"}</Mono>
            <Mono c={C.ibLo} s={12}>{st.ib?st.ib.L.toFixed(0):"—"}</Mono>
            <Mono c={C.dim} s={12}>{st.ib?st.ib.R.toFixed(0):"—"}</Mono>
            <Mono c={C.mid} s={12}>{st.ib?st.ib.M.toFixed(0):"—"}</Mono>
            <Mono c={C.dim} s={12}>{st.firstSideFormed||"—"}</Mono>
            <Mono c={st.firstBreak?C.warn:C.faint} s={12}>{st.firstBreak||"—"}</Mono>
            <PhaseTag phase={st.phase}/>
            <Mono c={sessions[i].vix>=cfg.vixMax?C.sell:C.dim} s={12}>{sessions[i].vix}</Mono>
            <Mono c={sigC} s={11} b>{sig}</Mono>
          </div>
        );
      })}
      <div style={{ padding:"10px 14px" }}><Mono c={C.faint} s={10}>click a row → Signals · sorted: armed first</Mono></div>
    </div>
  );
}

/* -------------------------- STRATEGY BUILDER ----------------------------- */
function Builder({ cfg, setCfg }) {
  const set = (k,v)=> setCfg(c=>({...c,[k]:v}));
  // synthetic preview IB
  const H=100, L=0, R=100, M=50, Q25=25, Q75=75, buf=8;
  let prev;
  if (cfg.mode==="REVERSION") {
    // assume low broke first → SELL fade
    prev = { side:"SELL", entry:M, stop:L-buf, tgt:H, note:"low broke first → fade up to 50%, target IB high" };
  } else {
    prev = { side:"BUY", entry:Q25, stop:M, tgt:H+R*0.5, note:"high break → buy 25% pullback, target 0.5× ext" };
  }
  const rr = Math.abs(prev.tgt-prev.entry)/Math.abs(prev.entry-prev.stop);
  const presets = [
    ["Source backtest", {mode:"REVERSION",breakMeasure:"WICK",entryLevel:"MID",weekday:"Tuesday",conditionFirstSide:"HIGH"}],
    ["NY-open fade 1:1", {mode:"REVERSION",breakMeasure:"WICK",entryLevel:"MID",stopPolicy:"STRUCTURAL",conditionFirstSide:"ANY"}],
    ["edgeful continuation", {mode:"CONTINUATION",breakMeasure:"CLOSE",entryLevel:"Q25",conditionFirstSide:"ANY"}],
  ];
  const fields = [
    ["mode","Trade thesis",["REVERSION","CONTINUATION"]],
    ["breakMeasure","Break measure",["WICK","CLOSE"]],
    ["entryLevel","Entry level",["MID","Q25","Q75"]],
    ["fillConfirm","Fill confirm",["TOUCH","CLOSE_REJECT","WICK_REJECT"]],
    ["stopPolicy","Stop policy",["STRUCTURAL","FIXED_RR"]],
    ["conditionFirstSide","Require side formed 1st",["ANY","HIGH","LOW"]],
    ["weekday","Weekday filter",["Mon","Tuesday","Wed","Thu","Fri","All"]],
  ];
  return (
    <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:18 }}>
      <div>
        <div style={{ display:"flex", gap:8, marginBottom:14, flexWrap:"wrap" }}>
          {presets.map(([n,p])=>(
            <button key={n} onClick={()=>setCfg(c=>({...c,...p}))} style={{ ...btn(C.cool), fontSize:11 }}>{n}</button>
          ))}
        </div>
        {fields.map(([k,label,opts])=>(
          <div key={k} style={{ marginBottom:14 }}>
            <Mono c={C.dim} s={10} style={{ letterSpacing:1.5, display:"block", marginBottom:6 }}>{label.toUpperCase()}</Mono>
            <div style={{ display:"flex", gap:6, flexWrap:"wrap" }}>
              {opts.map(o=>(
                <button key={o} onClick={()=>set(k,o)} style={{ background:cfg[k]===o?`${C.accent}1A`:C.panel,
                  color:cfg[k]===o?C.accent:C.dim, border:`1px solid ${cfg[k]===o?C.accent+"77":C.line}`,
                  borderRadius:6, padding:"7px 12px", fontFamily:MONO, fontSize:12, cursor:"pointer" }}>{o}</button>
              ))}
            </div>
          </div>
        ))}
        <div style={{ marginBottom:14 }}>
          <Mono c={C.dim} s={10} style={{ letterSpacing:1.5, display:"block", marginBottom:6 }}>VIX MAX (GATE)</Mono>
          <input type="range" min={12} max={28} value={cfg.vixMax} onChange={e=>set("vixMax",+e.target.value)}
            style={{ width:"100%", accentColor:C.accent }}/>
          <Mono c={C.accent} s={13} b>{cfg.vixMax}</Mono> <Mono c={C.faint} s={11}>skip day if India VIX ≥ this</Mono>
        </div>
      </div>

      {/* live preview */}
      <div style={{ background:C.panel, border:`1px solid ${C.line}`, borderRadius:10, padding:18 }}>
        <Mono c={C.dim} s={10} style={{ letterSpacing:2 }}>LIVE PREVIEW · {cfg.mode}</Mono>
        <div style={{ display:"flex", gap:20, marginTop:14, alignItems:"center" }}>
          <IBLadder day={{ ib:{H,L,R,M,Q25,Q75}, armed:prev, price:prev.entry }} h={240} w={150}/>
          <div style={{ flex:1 }}>
            <div style={{ display:"grid", gap:9 }}>
              <PV k="side" v={prev.side} c={prev.side==="SELL"?C.sell:C.buy}/>
              <PV k="entry" v={prev.entry.toFixed(0)} c={C.accent}/>
              <PV k="stop" v={cfg.stopPolicy==="FIXED_RR"? (prev.side==="SELL"?(prev.entry+(prev.tgt-prev.entry)*-1*-1/cfg.rrTarget).toFixed(0):"") : prev.stop.toFixed(0)} c={C.sell}/>
              <PV k="target" v={prev.tgt.toFixed(0)} c={C.buy}/>
              <PV k="RR" v={`${rr.toFixed(2)} : 1`} c={rr>=0.95?C.accent:C.warn}/>
            </div>
            <div style={{ marginTop:16, padding:12, background:C.panel2, borderRadius:8, border:`1px solid ${C.line}` }}>
              <Mono c={C.dim} s={11} style={{ lineHeight:1.6 }}>{prev.note}</Mono>
            </div>
            {rr < 0.8 && <div style={{ marginTop:10 }}><Mono c={C.warn} s={11}>⚠ RR below min — setup flagged sub-optimal</Mono></div>}
          </div>
        </div>
        <div style={{ marginTop:18, display:"flex", gap:8 }}>
          <button style={btn(C.accent)}>Save version</button>
          <button style={btn(C.dim)}>Clone &amp; tweak</button>
        </div>
      </div>
    </div>
  );
}
const PV = ({k,v,c}) => (
  <div style={{ display:"flex", justifyContent:"space-between", borderBottom:`1px solid ${C.line}55`, paddingBottom:7 }}>
    <Mono c={C.dim} s={12}>{k}</Mono><Mono c={c} s={15} b>{v}</Mono>
  </div>
);

/* --------------------------- STAT REPORTS -------------------------------- */
function StatReports() {
  // reproduces the edgeful chart: IB high formed first → which side breaks first
  const [cond, setCond] = useState("HIGH");
  const data = cond==="HIGH" ? {hi:31.25, lo:68.75} : {hi:64.1, lo:35.9};
  const filters = [["IB timeframe","09:15 – 10:15 IST"],["candle","5min"],["IB size","any"],
    ["break measure","by wick"],["ending zone","all days"],["weekdays","Tuesday"]];
  return (
    <div style={{ display:"grid", gridTemplateColumns:"1.3fr 1fr", gap:18 }}>
      <div style={{ background:C.panel, border:`1px solid ${C.line}`, borderRadius:10, padding:20 }}>
        <Mono c={C.dim} s={10} style={{ letterSpacing:2 }}>FIRST-BREAK SIDE · BY REJECTION</Mono>
        <div style={{ fontFamily:DISP, fontWeight:700, fontSize:16, margin:"6px 0 4px" }}>
          When IB {cond==="HIGH"?"high":"low"} forms first, which side breaks first?
        </div>
        <Mono c={C.faint} s={11}>NIFTY · 60min IB · simulated n=64 sessions</Mono>

        <div style={{ display:"flex", gap:8, marginTop:16 }}>
          {["HIGH","LOW"].map(o=>(
            <button key={o} onClick={()=>setCond(o)} style={{ background:cond===o?`${C.accent}1A`:C.panel2,
              color:cond===o?C.accent:C.dim, border:`1px solid ${cond===o?C.accent+"77":C.line}`,
              borderRadius:6, padding:"6px 12px", fontFamily:MONO, fontSize:11, cursor:"pointer" }}>IB {o} formed 1st</button>
          ))}
        </div>

        <div style={{ display:"flex", alignItems:"flex-end", gap:30, height:240, marginTop:24, padding:"0 30px" }}>
          {[["first break IB high",data.hi,C.cool],["first break IB low",data.lo,C.faint]].map(([l,v,c],i)=>(
            <div key={i} style={{ flex:1, display:"flex", flexDirection:"column", alignItems:"center" }}>
              <Mono c={C.ink} s={18} b style={{ marginBottom:8 }}>{v}%</Mono>
              <div style={{ width:"100%", height:`${v*2.6}px`, borderRadius:8,
                background: i===0 ? `linear-gradient(180deg,${C.cool},${C.cool}55)` : `linear-gradient(180deg,${C.line},${C.panel2})`,
                border:`1px solid ${i===0?C.cool:C.line}`, transition:"height .5s" }}/>
              <Mono c={C.dim} s={11} style={{ marginTop:10, textAlign:"center" }}>{l}</Mono>
            </div>
          ))}
        </div>
        <div style={{ marginTop:8, padding:12, background:`${C.accent}0A`, borderRadius:8, border:`1px solid ${C.accent}33` }}>
          <Mono c={C.accent} s={11} style={{ lineHeight:1.6 }}>
            {cond==="HIGH" ? "edge: high forms first but the LOW breaks first 68.75% → the first push fails → fade it (REVERSION)."
              : "low forms first → high break is the majority case → continuation reads cleaner here."}
          </Mono>
        </div>
      </div>

      <div style={{ background:C.panel, border:`1px solid ${C.line}`, borderRadius:10, padding:20 }}>
        <Mono c={C.dim} s={10} style={{ letterSpacing:2 }}>CUSTOM SETTINGS</Mono>
        <div style={{ marginTop:14, display:"grid", gap:10 }}>
          {filters.map(([k,v])=>(
            <div key={k} style={{ display:"flex", justifyContent:"space-between", borderBottom:`1px solid ${C.line}55`, paddingBottom:8 }}>
              <Mono c={C.faint} s={12}>{k}</Mono><Mono c={C.ink} s={12} b>{v}</Mono>
            </div>
          ))}
        </div>
        <div style={{ marginTop:18 }}>
          <Mono c={C.dim} s={10} style={{ letterSpacing:2 }}>RETRACEMENT HIT-RATE (single-break days)</Mono>
          <div style={{ marginTop:12, display:"grid", gap:9 }}>
            {[["25%",72,C.accent],["50%",41,C.cool],["75%",19,C.warn]].map(([l,v,c])=>(
              <div key={l}>
                <div style={{ display:"flex", justifyContent:"space-between", marginBottom:4 }}>
                  <Mono c={C.dim} s={11}>{l}</Mono><Mono c={c} s={11} b>{v}%</Mono>
                </div>
                <div style={{ height:7, background:C.panel2, borderRadius:4, overflow:"hidden" }}>
                  <div style={{ width:`${v}%`, height:"100%", background:c, transition:"width .5s" }}/>
                </div>
              </div>
            ))}
          </div>
          <Mono c={C.faint} s={10} style={{ display:"block", marginTop:12, lineHeight:1.5 }}>
            hover any stat → n, date range, filters. double-break days excluded.</Mono>
        </div>
      </div>
    </div>
  );
}

/* ----------------------------- SIGNALS ----------------------------------- */
function Signals({ states, sessions, cfg }) {
  const [sel, setSel] = useState(0);
  const st = states[sel], sess = sessions[sel];
  return (
    <div style={{ display:"grid", gridTemplateColumns:"1fr 320px", gap:18 }}>
      <div>
        <div style={{ display:"flex", gap:6, marginBottom:14 }}>
          {SYMBOLS.map((s,i)=>(
            <button key={s} onClick={()=>setSel(i)} style={{ background:sel===i?`${C.accent}1A`:C.panel,
              color:sel===i?C.accent:C.dim, border:`1px solid ${sel===i?C.accent+"77":C.line}`,
              borderRadius:6, padding:"7px 13px", fontFamily:MONO, fontSize:12, cursor:"pointer" }}>{s}</button>
          ))}
        </div>
        <div style={{ background:C.panel, border:`1px solid ${C.line}`, borderRadius:10, padding:20, display:"flex", gap:24 }}>
          <IBLadder day={st} h={300} w={170}/>
          <div style={{ flex:1 }}>
            <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:14 }}>
              <span style={{ fontFamily:DISP, fontWeight:700, fontSize:22 }}>{SYMBOLS[sel]}</span>
              <PhaseTag phase={st.phase}/>
            </div>
            <div style={{ display:"grid", gap:8 }}>
              <KV k="live price" v={st.price?st.price.toFixed(1):"—"} c={C.ink}/>
              <KV k="VIX gate" v={sess.vix>=cfg.vixMax?`BLOCKED ${sess.vix}`:`ok ${sess.vix}`} c={sess.vix>=cfg.vixMax?C.sell:C.accent}/>
              <KV k="formed 1st" v={st.firstSideFormed||"—"}/>
              <KV k="broke 1st" v={st.firstBreak||"—"} c={st.firstBreak?C.warn:C.dim}/>
            </div>
            {st.armed && (
              <div style={{ marginTop:16, padding:14, background:C.panel2, borderRadius:8, border:`1px solid ${st.armed.side==="SELL"?C.sell:C.buy}44` }}>
                <Mono c={st.armed.side==="SELL"?C.sell:C.buy} s={13} b>{st.armed.side} @ {st.armed.entry.toFixed(1)}</Mono>
                <div style={{ marginTop:8, display:"grid", gap:5 }}>
                  <KV k="stop" v={st.armed.stop.toFixed(1)} c={C.sell}/>
                  <KV k="target" v={st.armed.tgt.toFixed(1)} c={C.buy}/>
                  <KV k="RR" v={`${st.armed.rr.toFixed(2)} : 1`} c={C.accent}/>
                </div>
                <button style={{ ...btn(C.accent), marginTop:12, width:"100%" }}>Log paper fill</button>
              </div>
            )}
            {st.closed && (
              <div style={{ marginTop:16, padding:14, background:`${st.closed.pts>=0?C.buy:C.sell}10`,
                borderRadius:8, border:`1px solid ${st.closed.pts>=0?C.buy:C.sell}44` }}>
                <Mono c={st.closed.pts>=0?C.buy:C.sell} s={15} b>
                  {st.closed.reason} · {st.closed.pts>=0?"+":""}{st.closed.pts} pts · {st.closed.r}R</Mono>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* state-machine rail + log */}
      <div style={{ background:C.panel, border:`1px solid ${C.line}`, borderRadius:10, padding:18 }}>
        <Mono c={C.dim} s={10} style={{ letterSpacing:2 }}>STATE MACHINE</Mono>
        <div style={{ marginTop:12, display:"grid", gap:0 }}>
          {["BUILDING_IB","IB_READY","BREAK_CONFIRMED","ARMED","IN_TRADE","CLOSED"].map((p,i,arr)=>{
            const done = st.history.some(h=>h.to===p) || st.phase===p;
            const active = st.phase===p;
            return (
              <div key={p} style={{ display:"flex", gap:10, alignItems:"center", paddingBottom:i<arr.length-1?14:0, position:"relative" }}>
                {i<arr.length-1 && <div style={{ position:"absolute", left:5, top:14, width:1, height:18,
                  background: done?C.accentDim:C.line }}/>}
                <div style={{ width:11, height:11, borderRadius:"50%", flexShrink:0,
                  background: active?C.accent:done?C.accentDim:C.panel2,
                  border:`1px solid ${active?C.accent:done?C.accentDim:C.line}`,
                  boxShadow: active?`0 0 10px ${C.accent}`:"none", animation: active?"pulse 1.4s infinite":"none" }}/>
                <Mono c={active?C.accent:done?C.dim:C.faint} s={12} b={active}>{p.toLowerCase().replace("_"," ")}</Mono>
              </div>
            );
          })}
        </div>
        <Mono c={C.dim} s={10} style={{ letterSpacing:2, display:"block", margin:"18px 0 10px" }}>SIGNAL LOG</Mono>
        <div style={{ maxHeight:200, overflowY:"auto", display:"grid", gap:7 }}>
          {st.history.slice().reverse().map((h,i)=>(
            <div key={i} style={{ display:"flex", gap:8, fontFamily:MONO, fontSize:10.5 }}>
              <span style={{ color:C.faint }}>t{String(h.t).padStart(2,"0")}</span>
              <span style={{ color:C.dim }}>{h.reason}</span>
            </div>
          ))}
          {st.history.length===0 && <Mono c={C.faint} s={11}>waiting for session open…</Mono>}
        </div>
      </div>
    </div>
  );
}
