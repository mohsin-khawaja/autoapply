# ruff: noqa: E501 - the embedded HTML/JS template has long lines by nature
"""Local read-only web dashboard over the autoapply SQLite DB.

Serves a single-page HTML view (stat tiles, charts, jobs table) plus a JSON
endpoint. Read-only: no route mutates the DB. Bind is localhost-only.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _collect(db_path: Path) -> dict:
    """Aggregate everything the page needs in one read-only pass."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        one = lambda q, *p: conn.execute(q, p).fetchone()[0]  # noqa: E731
        total = one("SELECT COUNT(*) FROM jobs")
        active = one("SELECT COUNT(*) FROM jobs WHERE active=1 AND is_visible=1")
        hi = one("SELECT COUNT(*) FROM jobs WHERE active=1 AND is_visible=1 AND score>=70")
        apps = one("SELECT COUNT(*) FROM applications")
        submitted = one("SELECT COUNT(*) FROM applications WHERE status='submitted'")

        score_bins = [
            {"bin": r["b"], "count": r["c"]}
            for r in conn.execute(
                """SELECT (score/10)*10 AS b, COUNT(*) c FROM jobs
                   WHERE active=1 AND is_visible=1 GROUP BY b ORDER BY b"""
            )
        ]
        by_ats = [
            {"ats": r["a"] or "unknown", "count": r["c"]}
            for r in conn.execute(
                """SELECT ats a, COUNT(*) c FROM jobs WHERE active=1 AND is_visible=1
                   GROUP BY ats ORDER BY c DESC LIMIT 8"""
            )
        ]
        app_status = [
            {"status": r["s"], "count": r["c"]}
            for r in conn.execute(
                "SELECT status s, COUNT(*) c FROM applications GROUP BY s ORDER BY c DESC"
            )
        ]
        since = int((datetime.now(UTC) - timedelta(days=30)).timestamp())
        by_day = [
            {"day": r["d"], "count": r["c"]}
            for r in conn.execute(
                """SELECT date(date_posted,'unixepoch') d, COUNT(*) c FROM jobs
                   WHERE date_posted >= ? GROUP BY d ORDER BY d""",
                (since,),
            )
        ]
        app_rows = [
            dict(r)
            for r in conn.execute(
                """SELECT a.job_id, a.status, a.filled_at, a.submitted_at, a.screenshot, a.notes,
                          j.company_name, j.title, COALESCE(j.final_url, j.url) AS url
                   FROM applications a JOIN jobs j ON j.id = a.job_id
                   ORDER BY COALESCE(a.submitted_at, a.filled_at, '') DESC, a.id DESC"""
            )
        ]
        jobs = [
            dict(r)
            for r in conn.execute(
                """SELECT j.id, j.company_name, j.title, j.ats, j.score, j.locations,
                          j.url, j.final_url, j.date_posted, a.status app_status
                   FROM jobs j LEFT JOIN applications a ON a.job_id = j.id
                   WHERE j.active=1 AND j.is_visible=1
                   ORDER BY j.score DESC, j.date_posted DESC LIMIT 300"""
            )
        ]
        for j in jobs:
            j["locations"] = json.loads(j["locations"]) if j["locations"] else []
        return {
            "tiles": {
                "total": total,
                "active": active,
                "score70": hi,
                "applications": apps,
                "submitted": submitted,
            },
            "score_bins": score_bins,
            "by_ats": by_ats,
            "app_status": app_status,
            "by_day": by_day,
            "applications": app_rows,
            "jobs": jobs,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
    finally:
        conn.close()


def make_handler(db_path: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib API
            if self.path.split("?")[0] == "/shot":
                from urllib.parse import parse_qs, urlparse

                job = parse_qs(urlparse(self.path).query).get("job", [""])[0]
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                row = conn.execute(
                    "SELECT screenshot FROM applications WHERE job_id = ?", (job,)
                ).fetchone()
                conn.close()
                shot = Path(row[0]) if row and row[0] else None
                if shot is None or not shot.exists():
                    self.send_error(404)
                    return
                body = shot.read_bytes()
                ctype = "image/png"
            elif self.path.split("?")[0] == "/api/data":
                body = json.dumps(_collect(db_path)).encode()
                ctype = "application/json"
            elif self.path.split("?")[0] == "/":
                body = PAGE.encode()
                ctype = "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:  # quiet
            pass

    return Handler


def serve(db_path: Path, *, host: str = "127.0.0.1", port: int = 8787) -> None:
    """Blocking server loop. Ctrl-C to stop."""
    httpd = ThreadingHTTPServer((host, port), make_handler(db_path))
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>autoapply — dashboard</title>
<style>
:root{
  --surface:#fcfcfb; --page:#f9f9f7; --ink:#0b0b0b; --ink-2:#52514e;
  --muted:#898781; --grid:#e1e0d9; --axis:#c3c2b7; --ring:rgba(11,11,11,.10);
  --s1:#2a78d6; --s1-lo:#cde2fb; --good:#0ca30c; --warn:#fab219;
  --serious:#ec835a; --crit:#d03b3b;
}
@media (prefers-color-scheme: dark){:root{
  --surface:#1a1a19; --page:#0d0d0d; --ink:#ffffff; --ink-2:#c3c2b7;
  --muted:#898781; --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10);
  --s1:#3987e5; --s1-lo:#184f95;
}}
*{box-sizing:border-box;margin:0}
body{font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;
  background:var(--page);color:var(--ink);padding:28px;max-width:1180px;margin:0 auto}
header{display:flex;align-items:baseline;gap:12px;margin-bottom:20px}
h1{font-size:20px;font-weight:650}
header .sub{color:var(--muted);font-size:12.5px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:16px}
.tile{background:var(--surface);border:1px solid var(--ring);border-radius:10px;padding:14px 16px}
.tile .label{color:var(--ink-2);font-size:12.5px;margin-bottom:4px}
.tile .value{font-size:30px;font-weight:600;letter-spacing:-.01em}
.tile .note{color:var(--muted);font-size:12px;margin-top:2px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px}
@media(max-width:820px){.grid{grid-template-columns:1fr}}
.card{background:var(--surface);border:1px solid var(--ring);border-radius:10px;padding:16px}
.card h2{font-size:13px;font-weight:600;color:var(--ink-2);margin-bottom:12px}
svg text{font:11px system-ui,sans-serif;fill:var(--muted)}
svg .val{fill:var(--ink-2);font-weight:600}
.bar{fill:var(--s1)} .bar:hover{opacity:.85}
.empty{color:var(--muted);font-size:13px;padding:22px 0;text-align:center}
.controls{display:flex;gap:10px;margin-bottom:10px;align-items:center}
input[type=search]{background:var(--surface);border:1px solid var(--ring);border-radius:8px;
  padding:7px 10px;color:var(--ink);width:260px;font-size:13px}
select{background:var(--surface);border:1px solid var(--ring);border-radius:8px;
  padding:7px 8px;color:var(--ink);font-size:13px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{color:var(--muted);font-weight:600;text-align:left;padding:8px 10px;
  border-bottom:1px solid var(--grid);font-size:12px;cursor:pointer;user-select:none}
td{padding:8px 10px;border-bottom:1px solid var(--grid)}
td.num{font-variant-numeric:tabular-nums;text-align:right;font-weight:600}
tr:hover td{background:color-mix(in srgb,var(--s1) 6%,transparent)}
a{color:var(--s1);text-decoration:none}
code.copyid{cursor:pointer;font-size:11.5px;color:var(--muted);border:1px solid var(--ring);
  border-radius:6px;padding:2px 6px} code.copyid:hover{color:var(--ink)} a:hover{text-decoration:underline}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--ink-2)}
.pill::before{content:"";width:8px;height:8px;border-radius:50%;background:var(--dot,var(--muted))}
.count{color:var(--muted);font-size:12px;margin-left:auto}
#tip{position:fixed;pointer-events:none;background:var(--ink);color:var(--page);
  padding:5px 9px;border-radius:6px;font-size:12px;display:none;z-index:9}
</style></head><body>
<header><h1>autoapply</h1><span class="sub" id="stamp"></span></header>
<div class="tiles" id="tiles"></div>
<div class="grid">
  <div class="card"><h2>Score distribution — active jobs</h2><div id="hist"></div></div>
  <div class="card"><h2>Jobs by ATS</h2><div id="ats"></div></div>
  <div class="card"><h2>Postings per day — last 30 days</h2><div id="days"></div></div>
  <div class="card"><h2>Applications by status</h2><div id="apps"></div></div>
</div>
<div class="card" id="todocard" style="margin-bottom:16px">
  <div class="controls"><h2 style="margin:0">Finish manually — needs you</h2>
    <span class="count" id="tn"></span></div>
  <div id="todo"></div>
</div>
<div class="card" id="appcard" style="margin-bottom:16px">
  <div class="controls">
    <h2 style="margin:0">Applications</h2>
    <select id="fstatus"><option value="">All statuses</option></select>
    <span class="count" id="an"></span>
  </div>
  <div style="overflow-x:auto"><table id="atbl">
    <thead><tr><th>Status</th><th>Company</th><th>Title</th>
    <th>Filled</th><th>Submitted</th><th>Notes</th></tr></thead><tbody></tbody>
  </table></div>
</div>
<div class="card">
  <div class="controls">
    <h2 style="margin:0">Top jobs</h2>
    <input type="search" id="q" placeholder="Filter company / title…">
    <select id="fats"><option value="">All ATS</option></select>
    <span class="count" id="n"></span>
  </div>
  <div style="overflow-x:auto"><table id="tbl">
    <thead><tr><th data-k="score">Score</th><th data-k="company_name">Company</th>
    <th data-k="title">Title</th><th data-k="ats">ATS</th><th>Location</th>
    <th data-k="app_status">Application</th><th>Open</th></tr></thead><tbody></tbody>
  </table></div>
</div>
<div id="tip"></div>
<script>
const $=s=>document.querySelector(s), esc=s=>String(s??"").replace(/[&<>"]/g,
  c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const tip=$("#tip");
function hover(el,text){el.addEventListener("mousemove",e=>{tip.style.display="block";
  tip.textContent=text;tip.style.left=(e.clientX+12)+"px";tip.style.top=(e.clientY-28)+"px";});
  el.addEventListener("mouseleave",()=>tip.style.display="none");}

function tile(label,value,note){return `<div class="tile"><div class="label">${label}</div>
  <div class="value">${value.toLocaleString()}</div>${note?`<div class="note">${note}</div>`:""}</div>`}

function columns(el,data,xk,yk,fmt){ // vertical bars, direct-labeled caps
  if(!data.length){el.innerHTML='<div class="empty">no data yet</div>';return}
  const W=480,H=190,P={t:18,r:6,b:24,l:6},max=Math.max(...data.map(d=>d[yk]));
  const bw=Math.min(24,(W-P.l-P.r)/data.length-4);
  const x=i=>P.l+(W-P.l-P.r)*(i+.5)/data.length, y=v=>H-P.b-(H-P.t-P.b)*v/max;
  let s=`<svg viewBox="0 0 ${W} ${H}" width="100%">`;
  s+=`<line x1="${P.l}" x2="${W-P.r}" y1="${H-P.b}" y2="${H-P.b}" stroke="var(--axis)"/>`;
  data.forEach((d,i)=>{const h=H-P.b-y(d[yk]);
    s+=`<g class="col"><rect class="bar" x="${x(i)-bw/2}" y="${y(d[yk])}" width="${bw}"
      height="${Math.max(h,1)}" rx="4" ry="4"/>
      <rect x="${x(i)-bw/2}" y="${H-P.b-2}" width="${bw}" height="2" fill="var(--s1)"/>
      <text x="${x(i)}" y="${H-P.b+14}" text-anchor="middle">${fmt(d[xk])}</text>
      ${d[yk]===max?`<text class="val" x="${x(i)}" y="${y(d[yk])-5}" text-anchor="middle">${d[yk].toLocaleString()}</text>`:""}
      </g>`});
  el.innerHTML=s+"</svg>";
  el.querySelectorAll("g.col").forEach((g,i)=>hover(g,`${fmt(data[i][xk])}: ${data[i][yk].toLocaleString()}`));
}

function hbars(el,data,lk,vk,dot){ // horizontal bars, labels left, values at tip
  if(!data.length){el.innerHTML='<div class="empty">no data yet</div>';return}
  const W=480,rowH=26,P={l:110,r:56},max=Math.max(...data.map(d=>d[vk]));
  const H=data.length*rowH+6;
  let s=`<svg viewBox="0 0 ${W} ${H}" width="100%">`;
  data.forEach((d,i)=>{const w=Math.max((W-P.l-P.r)*d[vk]/max,2),y=i*rowH+5;
    const c=dot?dot(d[lk]):"var(--s1)";
    s+=`<g class="row"><text x="${P.l-8}" y="${y+12}" text-anchor="end" fill="var(--ink-2)">${esc(d[lk])}</text>
      <rect x="${P.l}" y="${y}" width="${w}" height="16" rx="4" ry="4" fill="${c}"/>
      <rect x="${P.l}" y="${y}" width="2" height="16" fill="${c}"/>
      <text class="val" x="${P.l+w+6}" y="${y+12}">${d[vk].toLocaleString()}</text></g>`});
  el.innerHTML=s+"</svg>";
  el.querySelectorAll("g.row").forEach((g,i)=>hover(g,`${data[i][lk]}: ${data[i][vk].toLocaleString()}`));
}

function line(el,data){
  if(data.length<2){el.innerHTML='<div class="empty">no data yet</div>';return}
  const W=480,H=190,P={t:14,r:10,b:22,l:34},max=Math.max(...data.map(d=>d.count));
  const x=i=>P.l+(W-P.l-P.r)*i/(data.length-1), y=v=>H-P.b-(H-P.t-P.b)*v/max;
  const pts=data.map((d,i)=>`${x(i)},${y(d.count)}`).join(" ");
  const area=`${P.l},${H-P.b} ${pts} ${W-P.r},${H-P.b}`;
  let s=`<svg viewBox="0 0 ${W} ${H}" width="100%">`;
  [0,.5,1].forEach(f=>{const v=Math.round(max*f);
    s+=`<line x1="${P.l}" x2="${W-P.r}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)"/>
        <text x="${P.l-6}" y="${y(v)+4}" text-anchor="end">${v.toLocaleString()}</text>`});
  s+=`<polygon points="${area}" fill="var(--s1)" opacity=".1"/>
      <polyline points="${pts}" fill="none" stroke="var(--s1)" stroke-width="2"
        stroke-linejoin="round" stroke-linecap="round"/>`;
  const last=data[data.length-1];
  s+=`<circle cx="${x(data.length-1)}" cy="${y(last.count)}" r="4" fill="var(--s1)"
        stroke="var(--surface)" stroke-width="2"/>
      <text class="val" x="${x(data.length-1)-6}" y="${y(last.count)-8}" text-anchor="end">${last.count.toLocaleString()}</text>
      <text x="${P.l}" y="${H-6}">${data[0].day}</text>
      <text x="${W-P.r}" y="${H-6}" text-anchor="end">${last.day}</text>`;
  el.innerHTML=s+"</svg>";
  const svg=el.querySelector("svg");
  svg.addEventListener("mousemove",e=>{const r=svg.getBoundingClientRect();
    const i=Math.round((e.clientX-r.left)/r.width*(data.length-1));
    const d=data[Math.max(0,Math.min(data.length-1,i))];
    tip.style.display="block";tip.textContent=`${d.day}: ${d.count.toLocaleString()}`;
    tip.style.left=(e.clientX+12)+"px";tip.style.top=(e.clientY-28)+"px";});
  svg.addEventListener("mouseleave",()=>tip.style.display="none");
}

let APPS=[];
function renderTodo(){
  const todo=APPS.filter(a=>a.status==="needs_input"||a.status==="manual");
  $("#tn").textContent=`${todo.length} waiting`;
  $("#todo").innerHTML=todo.length?todo.map(a=>{
    let miss=[]; try{miss=JSON.parse(a.notes||"[]")}catch(e){miss=a.notes?[a.notes]:[]}
    if(!Array.isArray(miss)) miss=[String(miss)];
    return `<div style="padding:10px 0;border-bottom:1px solid var(--grid)">
      <div style="display:flex;gap:10px;align-items:baseline;flex-wrap:wrap">
        <span class="pill" style="--dot:${STATUS_DOT[a.status]||"var(--muted)"}">${esc(a.status)}</span>
        <strong>${esc(a.company_name)}</strong>
        <a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.title)} ↗</a>
        ${a.screenshot?`<a href="/shot?job=${esc(a.job_id)}" target="_blank">screenshot</a>`:""}
      </div>
      ${miss.length?`<div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">${
        miss.slice(0,8).map(m=>`<span style="font-size:11.5px;color:var(--ink-2);border:1px solid var(--ring);border-radius:6px;padding:2px 8px">${esc(String(m).slice(0,60))}</span>`).join("")
      }${miss.length>8?`<span style="font-size:11.5px;color:var(--muted)">+${miss.length-8} more</span>`:""}</div>`:""}
    </div>`}).join(""):'<div class="empty">nothing waiting on you 🎉</div>';
}
function renderApps(){
  const fs=$("#fstatus").value;
  const rows=APPS.filter(a=>!fs||a.status===fs);
  $("#an").textContent=`${rows.length} of ${APPS.length}`;
  $("#atbl tbody").innerHTML=rows.length?rows.map(a=>`<tr>
    <td><span class="pill" style="--dot:${STATUS_DOT[a.status]||"var(--muted)"}">${esc(a.status)}</span></td>
    <td>${esc(a.company_name)}</td>
    <td><a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.title)}</a></td>
    <td class="num">${(a.filled_at||"—").slice(0,16).replace("T"," ")}</td>
    <td class="num">${(a.submitted_at||"—").slice(0,16).replace("T"," ")}</td>
    <td style="color:var(--muted);max-width:260px">${esc((a.notes||"").slice(0,80))}</td>
  </tr>`).join(""):'<tr><td colspan="6" class="empty">no applications yet — `autoapply queue add` + `autoapply run`</td></tr>';
}
const STATUS_DOT={submitted:"var(--good)",filled:"var(--s1)",queued:"var(--muted)",
  needs_input:"var(--warn)",failed:"var(--crit)",manual:"var(--serious)",
  skipped:"var(--muted)",messaged:"var(--s1)",deferred:"var(--warn)",replied:"var(--good)"};

let JOBS=[],sortK="score",sortDir=-1;
function renderTable(){
  const q=$("#q").value.toLowerCase(),fa=$("#fats").value;
  let rows=JOBS.filter(j=>(!fa||j.ats===fa)&&(!q||
    (j.company_name+" "+j.title).toLowerCase().includes(q)));
  rows.sort((a,b)=>{const x=a[sortK]??"",y=b[sortK]??"";
    return (x<y?-1:x>y?1:0)*sortDir});
  $("#n").textContent=`${rows.length} of ${JOBS.length}`;
  $("#tbl tbody").innerHTML=rows.slice(0,100).map(j=>`<tr>
    <td class="num">${j.score}</td>
    <td>${esc(j.company_name)}</td>
    <td><a href="${esc(j.final_url||j.url)}" target="_blank" rel="noopener">${esc(j.title)}</a></td>
    <td>${esc(j.ats||"—")}</td>
    <td>${esc(j.locations.slice(0,2).join(", ")||"—")}</td>
    <td>${j.app_status?`<span class="pill" style="--dot:${STATUS_DOT[j.app_status]||"var(--muted)"}">${esc(j.app_status)}</span>`:'<span style="color:var(--muted)">—</span>'}</td>
    <td><code class="copyid" data-id="${esc(j.id)}" title="click: copy \`autoapply open ${esc(j.id.slice(0,8))}\`">${esc(j.id.slice(0,8))}</code></td>
  </tr>`).join("");
  document.querySelectorAll(".copyid").forEach(c=>c.addEventListener("click",()=>{
    navigator.clipboard.writeText("uv run autoapply open "+c.dataset.id);
    c.textContent="copied!"; setTimeout(()=>c.textContent=c.dataset.id.slice(0,8),900);}));
}
$("#q").addEventListener("input",renderTable);
$("#fats").addEventListener("change",renderTable);
document.querySelectorAll("th[data-k]").forEach(th=>th.addEventListener("click",()=>{
  const k=th.dataset.k; sortDir=(k===sortK)?-sortDir:(k==="score"?-1:1); sortK=k; renderTable();}));

fetch("/api/data").then(r=>r.json()).then(d=>{
  $("#stamp").textContent="updated "+d.generated_at.replace("T"," ").replace("+00:00"," UTC");
  const t=d.tiles;
  $("#tiles").innerHTML=tile("Active jobs",t.active,`${t.total.toLocaleString()} total seen`)
    +tile("Score ≥ 70",t.score70,"strong matches")
    +tile("Applications",t.applications)
    +tile("Submitted",t.submitted);
  columns($("#hist"),d.score_bins,"bin","count",v=>v);
  hbars($("#ats"),d.by_ats,"ats","count");
  line($("#days"),d.by_day);
  hbars($("#apps"),d.app_status,"status","count",s=>STATUS_DOT[s]||"var(--muted)");
  APPS=d.applications||[];
  $("#fstatus").innerHTML='<option value="">All statuses</option>'+
    [...new Set(APPS.map(a=>a.status))].sort().map(x=>`<option>${esc(x)}</option>`).join("");
  $("#fstatus").addEventListener("change",renderApps);
  renderApps(); renderTodo();
  document.querySelectorAll("#tiles .tile")[2].style.cursor="pointer";
  document.querySelectorAll("#tiles .tile")[2].addEventListener("click",()=>{
    $("#fstatus").value=""; renderApps();
    $("#appcard").scrollIntoView({behavior:"smooth"});});
  $("#apps").querySelectorAll("g.row").forEach((g,i)=>{g.style.cursor="pointer";
    g.addEventListener("click",()=>{$("#fstatus").value=d.app_status[i].status;
      renderApps(); $("#appcard").scrollIntoView({behavior:"smooth"});});});
  JOBS=d.jobs; const ats=[...new Set(JOBS.map(j=>j.ats).filter(Boolean))].sort();
  $("#fats").innerHTML='<option value="">All ATS</option>'+ats.map(a=>`<option>${esc(a)}</option>`).join("");
  renderTable();
});
</script></body></html>
"""
