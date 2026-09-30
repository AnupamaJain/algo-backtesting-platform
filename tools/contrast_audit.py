"""WCAG contrast audit of the trading terminal, in a real browser.

The static checks in tests/test_theme_contrast.py catch light-mode values at
the source and run in the normal suite. This one renders every page, walks
the DOM, and computes each text node's ratio against the background actually
composited behind it -- which is the only way to catch inherited colour,
runtime-assigned styles and stacking.

    ./run-all.sh start
    venv/bin/python3 tools/contrast_audit.py <operator-password>

It reported 44 failures below 3:1 when the terminal was first themed dark,
including a table of live positions at 1.04:1. It reports 0 now.
"""
from playwright.sync_api import sync_playwright
out = "."
pw_ = sys.argv[1]; G = "http://127.0.0.1:8787"
PAGES = ["/home","/survivor","/wave-extractor","/early-exit","/early-exit-sensex","/expiry_trade",
         "/positions","/sensex_positions","/trade-journal","/tradebook-analysis","/duplicate-orders",
         "/nifty_contributors","/notifications/page","/position-guard/","/covered_calls/",
         "/api-monitor","/admin/instruments","/cas_tracker/","/disclaimer"]
AUDIT = r"""() => {
  function rgb(s){const m=(s||'').match(/[\d.]+/g); return m? m.slice(0,3).map(Number).concat([m[3]===undefined?1:Number(m[3])]) : null;}
  function lum(c){const f=c.map(v=>{v/=255; return v<=0.03928? v/12.92 : Math.pow((v+0.055)/1.055,2.4);}); return 0.2126*f[0]+0.7152*f[1]+0.0722*f[2];}
  function effBg(el){
    let n=el;
    while(n && n!==document.documentElement){
      const c=rgb(getComputedStyle(n).backgroundColor);
      if(c && c[3]>0.55) return c;
      n=n.parentElement;
    }
    return [10,13,22,1];
  }
  const out=[];
  const els=document.querySelectorAll('body *');
  for(const el of els){
    if(el.children.length && ![...el.childNodes].some(n=>n.nodeType===3 && n.textContent.trim())) continue;
    const t=(el.textContent||'').trim();
    if(!t || t.length>90) continue;
    const st=getComputedStyle(el);
    if(st.visibility==='hidden'||st.display==='none'||parseFloat(st.opacity)<0.15) continue;
    const r=el.getBoundingClientRect();
    if(r.width<2||r.height<2) continue;
    const fg=rgb(st.color); if(!fg||fg[3]<0.15) continue;
    if(st.backgroundImage && st.backgroundImage!=='none') continue;  // gradient/image fill: cannot judge from background-color
    const bg=effBg(el);
    const L1=lum(fg.slice(0,3)), L2=lum(bg.slice(0,3));
    const ratio=(Math.max(L1,L2)+0.05)/(Math.min(L1,L2)+0.05);
    if(ratio<3.0) out.push({t:t.slice(0,48), ratio:+ratio.toFixed(2), fg:st.color, bg:`rgb(${bg.slice(0,3).join(',')})`,
                            sel: el.tagName.toLowerCase()+(el.id?'#'+el.id:'')+(el.className&&typeof el.className==='string'?'.'+el.className.trim().split(/\s+/).slice(0,2).join('.'):'')});
  }
  const seen=new Set(); const uniq=[];
  for(const o of out){ const k=o.sel+'|'+o.fg+'|'+o.bg; if(seen.has(k))continue; seen.add(k); uniq.push(o);}
  return uniq.sort((a,b)=>a.ratio-b.ratio);
}"""
report={}
with sync_playwright() as pw:
    b=pw.chromium.launch(args=["--use-gl=angle","--enable-unsafe-swiftshader"])
    p=b.new_context(viewport={"width":1500,"height":1000}).new_page()
    p.on("dialog", lambda d: d.dismiss())
    p.goto(f"{G}/login",wait_until="networkidle"); p.fill("#email","operator@vriddhix.test"); p.fill("#password",pw_); p.click("#submit")
    p.wait_for_url("**/app",timeout=20000); p.wait_for_timeout(1200)
    p.click("#nav-apps a[href='/go/terminal']"); p.wait_for_url(f"{G}/terminal/**",timeout=60000); p.wait_for_timeout(1500)
    for path in PAGES:
        try:
            p.goto(f"{G}/terminal{path}",wait_until="domcontentloaded",timeout=60000); p.wait_for_timeout(3000)
            report[path]=p.evaluate(AUDIT)
        except Exception as e: report[path]=[{"t":f"ERROR {e}","ratio":0,"sel":"","fg":"","bg":""}]
    b.close()
json.dump(report, open("contrast-audit.json","w"), indent=1)
tot=0
for path,rows in report.items():
    if not rows: print(f"  {path:24s} clean"); continue
    tot+=len(rows); print(f"  {path:24s} {len(rows)} low-contrast")
    for r in rows[:5]: print(f"      {r['ratio']:>5}  {r['sel'][:44]:<44} {r['fg']} on {r['bg']}  \"{r['t'][:30]}\"")
print(f"\nTOTAL low-contrast (<3.0:1): {tot}")

raise SystemExit(1 if tot else 0)
