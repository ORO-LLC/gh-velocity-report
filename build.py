#!/usr/bin/env python3
"""Render every data/<year>.json into TWO self-contained files from one template:

  out/velocity.html           - a full standalone document for file:// use.
  out/velocity.artifact.html  - a FRAGMENT (no <!DOCTYPE>/<html>/<head>/<body>)
                                for publishing as a claude.ai Artifact, where the
                                publish skeleton supplies those tags. Both render
                                identically: same <style>, markup and JS.

Vanilla JS, no external scripts; stylesheets only from Google Fonts. A header
control switches years and a second control switches scope (the combined "all"
scorecard or one org/owner group); every section re-renders from the selected
scope's block. Everything is embedded inline so it works offline / from file://.

    python3 build.py
    python3 build.py --data-dir data --out out/velocity.html
    python3 build.py --redact-private        # share a page without private repo names
"""
import argparse
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent


def load_years(data_dir):
    years = {}
    for p in sorted(pathlib.Path(data_dir).glob("*.json")):
        try:
            d = json.loads(p.read_text())
            y = int(d["meta"]["year"])
            years[y] = d
        except Exception as e:
            print(f"skip {p}: {e}", file=sys.stderr)
    return years


def _blocks_of(d):
    """Every scope block of a year, new-format (d['scopes']) or old-format
    (the top level is the sole 'all' block)."""
    if d.get("scopes"):
        return list(d["scopes"].values())
    return [d]


def _redact_str(s, mapping):
    for full in sorted(mapping, key=len, reverse=True):
        s = s.replace(full, mapping[full])
    return s


def redact_private(years):
    """Replace private repo names with `<owner>/private-repo-N` (stable per build,
    owner preserved) in every scope block. Private OWNER names are NOT changed —
    only repo names. Names are rewritten in by_repo rows, in the precomputed
    insight sentences, and in meta.warnings, which are the only places a repo name
    appears in the JSON."""
    for d in years.values():
        blocks = _blocks_of(d)
        private = set()
        for b in blocks:
            for r in b.get("by_repo", []):
                if r.get("private"):
                    private.add(r["full_name"])
        if not private:
            continue
        mapping = {}
        for i, full in enumerate(sorted(private), 1):
            owner = full.split("/", 1)[0]
            mapping[full] = f"{owner}/private-repo-{i}"
        for b in blocks:
            for r in b.get("by_repo", []):
                if r["full_name"] in mapping:
                    red = mapping[r["full_name"]]
                    r["full_name"] = red
                    r["name"] = red.split("/", 1)[1]
            if b.get("insights"):
                b["insights"] = [_redact_str(s, mapping) for s in b["insights"]]
        m = d.get("meta", {})
        if m.get("warnings"):
            m["warnings"] = [_redact_str(w, mapping) for w in m["warnings"]]
    return years


TITLE = "<title>Personal Velocity</title>"
DESC = ('<meta name="description" content="A year of one person\'s GitHub contribution '
        'across every org and identity: commits, PRs, issues, reviews, releases, code, '
        'rhythm and streaks.">')
FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap">'
)

STYLE = r"""<style>
:root {
  --plane: #f4f6f9;
  --surface: #ffffff;
  --surface-2: #f8fafc;
  --ink: #12181f;
  --ink-2: #47515e;
  --muted: #6b7684;
  --rule: #dde3ea;
  --rule-soft: #eaeef3;
  --accent: #2a78d6;
  --accent-soft: #e8f1fc;
  --s1: #2a78d6;
  --s2: #eb6834;
  --s3: #1baf7a;
  --heat0: #eef2f6;
  --heat1: #cde2fb;
  --heat2: #86b6ef;
  --heat3: #3987e5;
  --heat4: #256abf;
  --heat5: #104281;
  --good: #0ca30c;
  --sans: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --plane: #0c1015;
    --surface: #141b23;
    --surface-2: #10161d;
    --ink: #e7ecf2;
    --ink-2: #aab6c4;
    --muted: #8592a1;
    --rule: #263140;
    --rule-soft: #1b232e;
    --accent: #4d94e8;
    --accent-soft: #14273d;
    --s1: #3987e5;
    --s2: #d95926;
    --s3: #199e70;
    --heat0: #1b232e;
    --heat1: #14345c;
    --heat2: #1c5cab;
    --heat3: #2a78d6;
    --heat4: #5598e7;
    --heat5: #9ec5f4;
    --good: #0ca30c;
  }
}
:root[data-theme="dark"] {
  --plane: #0c1015;
  --surface: #141b23;
  --surface-2: #10161d;
  --ink: #e7ecf2;
  --ink-2: #aab6c4;
  --muted: #8592a1;
  --rule: #263140;
  --rule-soft: #1b232e;
  --accent: #4d94e8;
  --accent-soft: #14273d;
  --s1: #3987e5;
  --s2: #d95926;
  --s3: #199e70;
  --heat0: #1b232e;
  --heat1: #14345c;
  --heat2: #1c5cab;
  --heat3: #2a78d6;
  --heat4: #5598e7;
  --heat5: #9ec5f4;
  --good: #0ca30c;
}
* { box-sizing: border-box; }
html, body { margin: 0; }
body {
  background: var(--plane);
  color: var(--ink);
  font-family: var(--sans);
  font-size: 15px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 1140px; margin: 0 auto; display: grid; gap: 26px; padding-inline: 16px; padding-block: 28px 56px; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }
h1 { font-size: 30px; line-height: 1.1; font-weight: 700; margin: 0; letter-spacing: -.02em; text-wrap: balance; }
h2 { font-size: 15px; font-weight: 600; margin: 0; text-transform: uppercase; letter-spacing: .06em; color: var(--ink-2); }
.eyebrow { font-family: var(--mono); font-size: 12px; letter-spacing: .1em; text-transform: uppercase; color: var(--muted); }
header { display: grid; gap: 12px; }
.controls {
  position: sticky; top: env(safe-area-inset-top, 0px); z-index: 10;
  background: var(--plane); display: flex; flex-wrap: wrap; gap: 10px 14px;
  align-items: center; padding: 8px 0; max-width: 100%;
}
.facts { display: flex; flex-wrap: wrap; gap: 4px 18px; font-family: var(--mono); font-size: 12.5px; color: var(--muted); }
.facts b { color: var(--ink); font-weight: 500; }
.head-note { font-family: var(--mono); font-size: 12px; color: var(--muted); }
.tabs { display: inline-flex; gap: 2px; padding: 3px; background: var(--surface-2); border: 1px solid var(--rule); border-radius: 999px; flex: none; }
.tabs button {
  font: inherit; font-family: var(--mono); font-size: 13px; font-weight: 500; cursor: pointer;
  border: 0; background: transparent; color: var(--ink-2); padding: 6px 16px; border-radius: 999px; line-height: 1.4;
}
.tabs button[aria-selected="true"] { background: var(--accent); color: #fff; }
.scope {
  display: flex; gap: 2px; padding: 3px; background: var(--surface-2); border: 1px solid var(--rule);
  border-radius: 999px; overflow-x: auto; max-width: 100%; scrollbar-width: thin; -webkit-overflow-scrolling: touch;
}
.scope button {
  font: inherit; font-family: var(--mono); font-size: 12px; font-weight: 500; cursor: pointer; border: 0;
  background: transparent; color: var(--ink-2); padding: 5px 12px; border-radius: 999px; line-height: 1.3;
  white-space: nowrap; display: inline-flex; align-items: center; gap: 6px; flex: none;
}
.scope button[aria-selected="true"] { background: var(--accent); color: #fff; }
.scope .scope-n { font-size: 10.5px; opacity: .65; font-variant-numeric: tabular-nums; }
.scope button[aria-selected="true"] .scope-n { opacity: .85; }
section.card { background: var(--surface); border: 1px solid var(--rule); border-radius: 10px; padding: 20px 22px; display: grid; gap: 16px; }
.sec-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 14px; justify-content: space-between; }
.sec-head .hint { font-size: 12.5px; color: var(--muted); }
.tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(168px, 1fr)); gap: 10px; }
.tile { display: grid; gap: 3px; align-content: start; padding: 13px 14px; border: 1px solid var(--rule-soft); border-radius: 8px; background: var(--surface-2); }
.tile .k { font-family: var(--mono); font-size: 11px; letter-spacing: .05em; text-transform: uppercase; color: var(--muted); }
.tile .v { font-size: 22px; font-weight: 600; letter-spacing: -.02em; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.tile .s { font-size: 12px; color: var(--muted); }
.legend { display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 12.5px; color: var(--ink-2); }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.swatch { width: 11px; height: 11px; border-radius: 3px; display: inline-block; }
.chart { width: 100%; overflow-x: auto; }
svg { display: block; max-width: 100%; }
svg text { fill: var(--ink-2); font-family: var(--mono); }
.two-col { display: grid; grid-template-columns: minmax(0,1fr) minmax(0,2fr); gap: 18px 30px; align-items: start; }
.plain { margin: 0; padding: 0; list-style: none; display: grid; gap: 6px; font-size: 13.5px; }
.plain .mono { font-family: var(--mono); font-variant-numeric: tabular-nums; color: var(--ink-2); }
.insights { margin: 0; padding-left: 18px; display: grid; gap: 7px; font-size: 14px; }
.tablewrap { overflow-x: auto; border: 1px solid var(--rule-soft); border-radius: 8px; }
table { border-collapse: collapse; width: 100%; font-size: 13.5px; min-width: 720px; }
table.wide { min-width: 980px; }
thead th { position: sticky; top: 0; background: var(--surface); text-align: right; font-family: var(--mono); font-weight: 500; font-size: 11px; letter-spacing: .04em; text-transform: uppercase; color: var(--muted); padding: 9px 11px; border-bottom: 1px solid var(--rule); white-space: nowrap; cursor: pointer; user-select: none; }
thead th:first-child, thead th.l { text-align: left; }
thead th.sorted::after { content: " \25be"; color: var(--accent); }
thead th.asc::after { content: " \25b4"; color: var(--accent); }
tbody td { padding: 8px 11px; border-bottom: 1px solid var(--rule-soft); text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
tbody td.l { text-align: left; }
tbody tr:last-child td { border-bottom: 0; }
tbody tr:hover { background: var(--accent-soft); }
tbody tr.here td { background: var(--accent-soft); font-weight: 600; }
tfoot td { padding: 9px 11px; border-top: 1px solid var(--rule); font-weight: 600; text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
tfoot td.l { text-align: left; }
.badge { font-family: var(--mono); font-size: 10px; padding: 1px 6px; border-radius: 4px; border: 1px solid var(--rule); color: var(--muted); margin-left: 6px; }
.bar-mini { height: 6px; border-radius: 3px; background: var(--accent); display: inline-block; vertical-align: middle; min-width: 1px; }
.filter-row { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; justify-content: flex-end; }
input[type="search"] { font: inherit; font-size: 13px; padding: 6px 11px; border: 1px solid var(--rule); border-radius: 8px; background: var(--surface-2); color: var(--ink); min-width: 180px; }
.callout { border: 1px solid var(--rule); border-left: 3px solid var(--accent); border-radius: 8px; padding: 12px 14px; background: var(--surface-2); font-size: 13px; display: grid; gap: 6px; }
.callout.warn { border-left-color: var(--s2); }
.callout.context { border-left-color: var(--muted); }
.callout code { font-family: var(--mono); font-size: 12px; background: var(--rule-soft); padding: 1px 4px; border-radius: 3px; }
.ctx-nums { display: flex; flex-wrap: wrap; gap: 6px 22px; font-family: var(--mono); font-size: 13px; }
.ctx-nums b { font-size: 17px; }
.notes-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px 26px; font-size: 13px; color: var(--ink-2); }
.notes-grid dt { font-weight: 600; color: var(--ink); }
.notes-grid dd { margin: 2px 0 0; color: var(--muted); }
footer { font-size: 12.5px; color: var(--muted); text-align: center; }
.tip { position: fixed; pointer-events: none; z-index: 20; background: var(--ink); color: var(--surface); font-family: var(--mono); font-size: 11.5px; padding: 5px 8px; border-radius: 6px; opacity: 0; transition: opacity .08s; max-width: 260px; }
@media (max-width: 720px) {
  .wrap { padding-block: 20px 40px; }
  h1 { font-size: 25px; }
  section.card { padding: 16px 15px; }
  .two-col, .notes-grid { grid-template-columns: 1fr; }
}
@media (prefers-reduced-motion: reduce) { * { transition: none !important; animation: none !important; } }
</style>"""

BODY = r"""<div class="wrap">
  <header>
    <div class="eyebrow" id="eyebrow">Personal velocity</div>
    <h1 id="title">Personal velocity</h1>
    <div class="controls" id="controls">
      <div class="tabs" id="tabs" role="tablist" aria-label="Year"></div>
      <div class="scope" id="scope" role="tablist" aria-label="Scope (org or owner)"></div>
    </div>
    <div class="facts" id="facts"></div>
    <div class="head-note" id="head-note"></div>
  </header>
  <main id="main"></main>
  <footer>
    Counts are read-only from GitHub. Line and commit counts come from git on each repo's default branch, merges excluded, bot commits excluded by identity matching. See "How these numbers are counted".
  </footer>
  <div class="tip" id="tip"></div>
</div>
<script id="data" type="application/json">__DATA__</script>
""" + r"""<script>
"use strict";
const DATA = JSON.parse(document.getElementById("data").textContent);
const YEARS = Object.keys(DATA).map(Number).sort((a,b)=>a-b);
const MON = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const WD = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"];
const N = n => (n==null? "—" : Number(n).toLocaleString("en-US"));
const P = x => (x==null? "—" : Math.round(x*100)+"%");
const el = (t, a, ...kids) => { const e=document.createElement(t); if(a) for(const k in a){ if(k==="class") e.className=a[k]; else if(k==="html") e.innerHTML=a[k]; else e.setAttribute(k,a[k]); } for(const c of kids){ if(c==null) continue; e.append(c.nodeType? c : document.createTextNode(c)); } return e; };
const NS = "http://www.w3.org/2000/svg";
const svg = (t,a)=>{ const e=document.createElementNS(NS,t); for(const k in a) e.setAttribute(k,a[k]); return e; };

const tip = document.getElementById("tip");
function showTip(ev, html){ tip.innerHTML=html; tip.style.opacity=1; moveTip(ev); }
function moveTip(ev){ const x=ev.clientX, y=ev.clientY; tip.style.left=Math.min(x+12, innerWidth-tip.offsetWidth-8)+"px"; tip.style.top=(y+16)+"px"; }
function hideTip(){ tip.style.opacity=0; }

function hoursText(h){ if(h==null) return "—"; if(h<1) return Math.round(h*60)+" min"; if(h<10) return h.toFixed(1)+" h"; if(h<48) return Math.round(h)+" h"; return (h/24).toFixed(1)+" d"; }
function fmtDate(iso){ if(!iso) return "—"; const d=new Date(iso+"T00:00:00"); return MON[d.getMonth()]+" "+d.getDate(); }
function pm(a,b){ return "+"+N(a)+" / −"+N(b); }
function signed(n){ return (n<0? "−":"+")+N(Math.abs(n)); }
function elapsedMonths(m){ try{ return new Date(m.through+"T00:00:00").getMonth()+1; }catch(e){ return 12; } }

// ---- scope handling (with backward compatibility for pre-scopes files) ----
function legacyBlock(d){ return {totals:d.totals, insights:d.insights, by_persona:d.by_persona, by_org:d.by_org, by_repo:d.by_repo, by_month:d.by_month, by_identity:d.by_identity, calendar:d.calendar, by_weekday_hour:d.by_weekday_hour, streaks:d.streaks, context:d.context}; }
function scopesOf(d){ return (d.scopes && Object.keys(d.scopes).length)? d.scopes : {all: legacyBlock(d)}; }
function groupsOrdered(scopes){ return Object.keys(scopes).filter(k=>k!=="all").sort((a,b)=> (((scopes[b].totals||{}).commits||0)-((scopes[a].totals||{}).commits||0)) || a.localeCompare(b)); }

let current = {year:null, scope:"all"};

function buildScopeControl(scopes, active){
  const cont=document.getElementById("scope");
  cont.innerHTML="";
  const order=["all", ...groupsOrdered(scopes)];
  for(const s of order){
    const b=el("button",{type:"button",role:"tab","data-s":s,"aria-selected":String(s===active),"aria-label":(s==="all"?"All":s)+" scope"});
    b.append(el("span",{class:"scope-l"}, s==="all"?"All":s));
    const c=((scopes[s]||{}).totals||{}).commits;
    b.append(el("span",{class:"scope-n"}, N(c==null?0:c)));
    b.addEventListener("click",()=>{ writeHash(current.year, s); render(current.year, s); });
    cont.append(b);
  }
}

function render(year, scope){
  const d = DATA[year];
  const scopes = scopesOf(d);
  if(!scope || !scopes[scope]) scope="all";
  current = {year, scope};
  const m = d.meta;
  const block = scopes[scope];
  const view = Object.assign({meta:m}, block);
  view.by_org = (scopes.all||{}).by_org || block.by_org || [];
  view._scope = scope;

  document.getElementById("eyebrow").textContent = m.display_name || "Personal velocity";
  document.getElementById("title").textContent = "Personal velocity · " + year + (scope!=="all"? " · " + scope : "");
  for(const b of document.querySelectorAll("#tabs button")) b.setAttribute("aria-selected", String(Number(b.dataset.y)===year));
  buildScopeControl(scopes, scope);

  const facts = document.getElementById("facts");
  facts.innerHTML = "";
  facts.append(
    el("span",{html:"through <b>"+fmtDate(m.through)+", "+year+"</b>"}),
    el("span",{html:"identities <b>"+m.identities.emails.length+" emails · "+m.identities.logins.length+" logins</b>"}),
    el("span",{html:"accounts <b>"+m.accounts_used.join(", ")+"</b>"}),
    el("span",{html:"generated <b>"+(m.generated_at||"").slice(0,10)+"</b>"})
  );
  document.getElementById("head-note").textContent =
    "as of "+m.through+" · "+(scope==="all"? "all groups combined" : "scope: "+scope)+" · default branches · merges excluded · bot commits excluded by identity matching";

  const main = document.getElementById("main");
  main.innerHTML = "";
  main.append(tilesSection(view), insightsSection(view), monthlySection(view), calendarSection(view),
              orgSection(view), repoSection(view), rhythmSection(view), accountsSection(view),
              contextSection(view), notesSection(view));
}

function card(title, hint, ...body){
  const head = el("div",{class:"sec-head"}, el("h2",null,title), hint? el("span",{class:"hint"},hint): null);
  return el("section",{class:"card"}, head, ...body.filter(Boolean));
}

function tilesSection(d){
  const t=d.totals, s=d.streaks, m=d.meta, r=t.ratios||{};
  const scanned = m.repos_scanned_for_commits!=null? m.repos_scanned_for_commits : m.repos_scanned;
  const tiles=[
    ["Commits", N(t.commits), "default branch · merges excluded"],
    ["Pull requests", N(t.prs_opened)+" / "+N(t.prs_merged), "opened / merged · "+N(t.prs_closed_unmerged)+" closed unmerged · "+P(r.merge_rate)+" merge rate"],
    ["Issues", N(t.issues_opened)+" / "+N(t.issues_closed), "opened / closed, authored"],
    ["Reviews given", N(t.reviews), N(t.self_reviews_excluded||0)+" self-reviews excluded"],
    ["Releases", N(t.releases), "published this year"],
    ["Active repos", N(t.repos), d._scope==="all"? "of "+N(scanned)+" scanned for commits" : "in this scope"],
    ["Orgs", N(t.orgs), "configured orgs touched"],
    ["Active days", N(t.active_days), (r.commits_per_active_day!=null? N(r.commits_per_active_day)+" commits/day":"with ≥1 commit")],
    ["Longest streak", N(s.longest)+" d", (s.longest_start? s.longest_start+" → "+s.longest_end : "current "+N(s.current)+" d")],
    ["Code lines", pm(t.code_add,t.code_del), "net "+signed(t.code_net)],
    ["Docs lines", pm(t.docs_add,t.docs_del), "net "+signed(t.docs_net)+" · "+N(t.docs_files_add)+" files added, "+N(t.docs_files_del)+" removed"],
    ["Identities", N((m.identities.personas||[]).length)+" people", N(m.identities.emails.length)+" author emails and "+N(m.identities.logins.length)+" logins folded to one scorecard"],
    ["Median PR cycle", hoursText(t.cycle_time.median_hours), "p90 "+hoursText(t.cycle_time.p90_hours)+" · n="+N(t.cycle_time.population)],
    ["Monthly pace", N(r.avg_issues_closed_per_month)+" · "+N(r.avg_prs_merged_per_month), "avg issues closed · PRs merged / month ("+N(r.elapsed_months)+" mo)"]
  ];
  const grid=el("div",{class:"tiles"});
  for(const [k,v,s2] of tiles) grid.append(el("div",{class:"tile"}, el("div",{class:"k"},k), el("div",{class:"v"},v), el("div",{class:"s"},s2)));
  return card("At a glance", d._scope==="all"? null : "scope: "+d._scope, grid);
}

function insightsSection(d){
  const ins=d.insights||[];
  const em=elapsedMonths(d.meta);
  const recent=d.by_month.filter(x=>x.month<=em && (x.commits||x.issues_closed||x.prs_merged)).slice(-4);
  const left=el("div",null, el("h3",{style:"font-size:13px;margin:0 0 8px;color:var(--ink-2)"},"Last four months"),
    el("ul",{class:"plain"}, ...(recent.length? recent.map(x=>el("li",null,
      el("span",{style:"font-weight:600"}, MON[x.month-1]+" "), el("span",{class:"mono"}, N(x.issues_closed)+" iss · "+N(x.prs_merged)+" PRs · "+N(x.commits)+" commits")))
      : [el("li",{class:"mono"},"no month with activity yet")])));
  const right=el("div",null, el("h3",{style:"font-size:13px;margin:0 0 8px;color:var(--ink-2)"},"Insights"),
    el("ul",{class:"insights"}, ...ins.map(s=>el("li",null,s))));
  if(!ins.length) return null;
  return card("Insights", null, el("div",{class:"two-col"}, left, right));
}

function legend(items){
  const w=el("div",{class:"legend"});
  for(const [c,l] of items) w.append(el("span",null, el("span",{class:"swatch",style:"background:"+c}), l));
  return w;
}

const MONTH_COLS=[
  ["month","Month","l",x=>MON[x.month-1]],
  ["commits","Commits","",x=>N(x.commits)],
  ["prs_opened","PRs open","",x=>N(x.prs_opened)],
  ["prs_merged","PRs mg","",x=>N(x.prs_merged)],
  ["prs_closed_unmerged","PRs unmg","",x=>N(x.prs_closed_unmerged)],
  ["issues_opened","Iss open","",x=>N(x.issues_opened)],
  ["issues_closed","Iss closed","",x=>N(x.issues_closed)],
  ["reviews","Reviews","",x=>N(x.reviews)],
  ["releases","Rel","",x=>N(x.releases)],
  ["code","Code +/−","",x=>pm(x.code_add,x.code_del)],
  ["docs","Docs +/−","",x=>pm(x.docs_add,x.docs_del)],
  ["dfiles","Docs files +/−","",x=>"+"+N(x.docs_files_add)+" / −"+N(x.docs_files_del)]
];

function monthlySection(d){
  const bm=d.by_month;
  const series=[["commits","var(--s1)","Commits"],["prs_merged","var(--s2)","PRs merged"],["issues_closed","var(--s3)","Issues closed"]];
  const W=760,H=240, padL=44, padB=28, padT=10, padR=8;
  const iw=W-padL-padR, ih=H-padT-padB;
  const max=Math.max(1, ...bm.flatMap(x=>series.map(s=>x[s[0]])));
  const s=svg("svg",{viewBox:`0 0 ${W} ${H}`,role:"img","aria-label":"Monthly commits, PRs merged and issues closed"});
  const ticks=4;
  for(let i=0;i<=ticks;i++){ const y=padT+ih*i/ticks; const val=Math.round(max*(1-i/ticks));
    s.append(svg("line",{x1:padL,y1:y,x2:W-padR,y2:y,stroke:"var(--rule-soft)","stroke-width":1}));
    const tx=svg("text",{x:padL-6,y:y+3,"text-anchor":"end","font-size":10}); tx.textContent=N(val); s.append(tx); }
  const bw=iw/12, gw=bw*0.7, sw=gw/series.length;
  bm.forEach((mo,mi)=>{
    const x0=padL+bw*mi+(bw-gw)/2;
    series.forEach((se,si)=>{
      const v=mo[se[0]]||0; const h=ih*v/max; const x=x0+sw*si;
      const rr=svg("rect",{x:x+0.6,y:padT+ih-h,width:Math.max(0,sw-1.2),height:h,fill:se[1],rx:2});
      rr.addEventListener("mousemove",ev=>showTip(ev,`<b>${MON[mi]} ${d.meta.year}</b><br>${se[2]}: ${N(v)}<br>Releases: ${N(mo.releases)} · PRs unmerged: ${N(mo.prs_closed_unmerged)}`));
      rr.addEventListener("mouseleave",hideTip);
      s.append(rr);
    });
    const tx=svg("text",{x:padL+bw*mi+bw/2,y:H-padB+14,"text-anchor":"middle","font-size":10}); tx.textContent=MON[mi]; s.append(tx);
  });
  const em=elapsedMonths(d.meta);
  const shown=bm.filter(x=>x.month<=em);
  const tbl=el("table",{class:"wide"}); const hr=el("tr");
  for(const [key,label,cls] of MONTH_COLS) hr.append(el("th",{class:cls||""},label));
  tbl.append(el("thead",null,hr));
  const tb=el("tbody");
  for(const x of shown){ const tr=el("tr"); for(const [key,label,cls,fn] of MONTH_COLS) tr.append(el("td",{class:cls||""},fn(x))); tb.append(tr); }
  tbl.append(tb);
  const tot={month:0,commits:0,prs_opened:0,prs_merged:0,prs_closed_unmerged:0,issues_opened:0,issues_closed:0,reviews:0,releases:0,code_add:0,code_del:0,docs_add:0,docs_del:0,docs_files_add:0,docs_files_del:0};
  for(const x of shown) for(const k in tot) if(k!=="month") tot[k]+=x[k]||0;
  const fr=el("tr");
  fr.append(el("td",{class:"l"},"Total"));
  for(const [key,label,cls,fn] of MONTH_COLS.slice(1)) fr.append(el("td",{class:cls||""}, fn(tot)));
  tbl.append(el("tfoot",null,fr));
  return card("Monthly activity", "chart is commits / PRs merged / issues closed; table has every counter",
    legend(series.map(x=>[x[1],x[2]])),
    el("div",{class:"chart"}, s),
    el("div",{class:"tablewrap"}, tbl));
}

function calendarSection(d){
  const cal=d.calendar||{}; const year=d.meta.year;
  const start=new Date(year,0,1); const end=new Date(year,11,31);
  const gridStart=new Date(start); gridStart.setDate(start.getDate()-(start.getDay()));
  const days=[]; for(let dtv=new Date(gridStart); dtv<=end; dtv.setDate(dtv.getDate()+1)) days.push(new Date(dtv));
  const weeks=Math.ceil(days.length/7);
  let max=1; for(const k in cal) max=Math.max(max,cal[k].commits);
  const cell=13, gap=3, padL=30, padT=18;
  const W=padL+weeks*(cell+gap), H=padT+7*(cell+gap)+4;
  const s=svg("svg",{viewBox:`0 0 ${W} ${H}`,role:"img","aria-label":"Contribution calendar heatmap"});
  const heat=v=>{ if(!v) return "var(--heat0)"; const q=v/max; if(q<=0.15) return "var(--heat1)"; if(q<=0.35) return "var(--heat2)"; if(q<=0.6) return "var(--heat3)"; if(q<=0.85) return "var(--heat4)"; return "var(--heat5)"; };
  let lastMonth=-1;
  days.forEach((dtv,i)=>{
    const wk=Math.floor(i/7), wd=i%7;
    const iso=dtv.getFullYear()+"-"+String(dtv.getMonth()+1).padStart(2,"0")+"-"+String(dtv.getDate()).padStart(2,"0");
    const inYear=dtv.getFullYear()===year;
    const rec=cal[iso]; const cm=rec?rec.commits:0; const act=rec?rec.activity:0;
    const x=padL+wk*(cell+gap), y=padT+wd*(cell+gap);
    const rr=svg("rect",{x,y,width:cell,height:cell,rx:3,fill:inYear?heat(cm):"transparent","stroke":"var(--rule-soft)","stroke-width":inYear?0.5:0});
    if(inYear){ rr.addEventListener("mousemove",ev=>showTip(ev,`<b>${iso}</b><br>${N(cm)} commits · ${N(act)} activity`)); rr.addEventListener("mouseleave",hideTip); }
    s.append(rr);
    if(inYear && dtv.getMonth()!==lastMonth && wd===0){ lastMonth=dtv.getMonth(); const tx=svg("text",{x,y:padT-6,"font-size":9}); tx.textContent=MON[dtv.getMonth()]; s.append(tx); }
  });
  ["Mon","Wed","Fri"].forEach(l=>{ const wd=WD.indexOf(l)+1; const y=padT+wd*(cell+gap)+cell-3; const tx=svg("text",{x:0,y,"font-size":9}); tx.textContent=l; s.append(tx); });
  const bd=d.streaks.busiest_day;
  return card("Contribution calendar", (bd&&bd.date)? "busiest day "+bd.date+" ("+N(bd.commits)+" commits)": null,
    el("div",{class:"chart"}, s),
    legend([["var(--heat1)","less"],["var(--heat3)","more"],["var(--heat5)","most"]]));
}

function barCell(v,max){ const pct=max? Math.round(100*v/max):0; return el("span",{class:"bar-mini",style:"width:"+Math.max(pct*0.7,v?2:0)+"px"}); }

function orgSection(d){
  const rows=[...d.by_org].sort((a,b)=>b.commits-a.commits);
  const max=Math.max(1,...rows.map(r=>r.commits));
  const cols=[
    ["Group","l",r=>r.group],
    ["Repos","",r=>N(r.repos)],
    ["Commits","",r=>N(r.commits)],
    ["","l",r=>barCell(r.commits,max)],
    ["Code +/−","",r=>pm(r.code_add,r.code_del)],
    ["Docs +/−","",r=>pm(r.docs_add,r.docs_del)],
    ["Docs files +/−","",r=>"+"+N(r.docs_files_add)+" / −"+N(r.docs_files_del)],
    ["PRs mg","",r=>N(r.prs_merged)],
    ["PRs unmg","",r=>N(r.prs_closed_unmerged)],
    ["Iss closed","",r=>N(r.issues_closed)],
    ["Reviews","",r=>N(r.reviews)],
    ["Rel","",r=>N(r.releases)]
  ];
  const tbl=el("table",{class:"wide"}); const hr=el("tr");
  for(const [label,cls] of cols) hr.append(el("th",{class:cls||""},label));
  tbl.append(el("thead",null,hr));
  const tb=el("tbody");
  for(const r of rows){ const tr=el("tr", r.group===d._scope? {class:"here"}: null); for(const [label,cls,fn] of cols) tr.append(el("td",{class:cls||""}, fn(r))); tb.append(tr); }
  tbl.append(tb);
  const hint = d._scope==="all"? "click a scope above to focus one group" : "the selected scope is highlighted";
  return card("By org / owner", hint, el("div",{class:"tablewrap"}, tbl));
}

const REPO_COLS=[
  ["full_name","Repo","l",r=>repoLink(r)],
  ["commits","Commits","",r=>N(r.commits)],
  ["code_add","+code","",r=>N(r.code_add)],
  ["code_del","−code","",r=>N(r.code_del)],
  ["docs_add","+docs","",r=>N(r.docs_add)],
  ["docs_del","−docs","",r=>N(r.docs_del)],
  ["files_add","+files","",r=>N((r.code_files_add||0)+(r.docs_files_add||0))],
  ["files_del","−files","",r=>N((r.code_files_del||0)+(r.docs_files_del||0))],
  ["prs_opened","PRs open","",r=>N(r.prs_opened)],
  ["prs_merged","PRs mg","",r=>N(r.prs_merged)],
  ["prs_closed_unmerged","PRs unmg","",r=>N(r.prs_closed_unmerged)],
  ["issues_opened","Iss open","",r=>N(r.issues_opened)],
  ["issues_closed","Iss closed","",r=>N(r.issues_closed)],
  ["reviews","Reviews","",r=>N(r.reviews)],
  ["releases","Rel","",r=>N(r.releases)],
  ["first","First","",r=>fmtDate(r.first)],
  ["last","Last","",r=>fmtDate(r.last)]
];
function sortKey(r,k){
  if(k==="files_add") return (r.code_files_add||0)+(r.docs_files_add||0);
  if(k==="files_del") return (r.code_files_del||0)+(r.docs_files_del||0);
  return r[k];
}
function repoLink(r){
  const a=el("a",{href:"https://github.com/"+r.full_name,target:"_blank",rel:"noopener"}, r.full_name);
  const wrap=el("span",null,a);
  if(r.private) wrap.append(el("span",{class:"badge"},"private"));
  if(r.fork) wrap.append(el("span",{class:"badge"},"fork"));
  if(r.archived) wrap.append(el("span",{class:"badge"},"archived"));
  return wrap;
}
let repoState={sort:"commits",asc:false,q:""};
function repoSection(d){
  repoState={sort:"commits",asc:false,q:""};
  const search=el("input",{type:"search",id:"repo-filter",placeholder:"filter repos…","aria-label":"Filter repos"});
  search.addEventListener("input",()=>{ repoState.q=search.value.toLowerCase(); draw(); });
  const tblwrap=el("div",{class:"tablewrap"});
  const sec=card("By repo", d.by_repo.length+" repos with activity · click a header to sort, scroll for more columns",
    el("div",{class:"filter-row"}, search), tblwrap);
  const maxC=Math.max(1,...d.by_repo.map(r=>r.commits));
  function draw(){
    let rows=d.by_repo.filter(r=> (!repoState.q||r.full_name.toLowerCase().includes(repoState.q)));
    rows.sort((a,b)=>{ const k=repoState.sort; let av=sortKey(a,k),bv=sortKey(b,k); if(typeof av==="string"){ av=av||""; bv=bv||""; return repoState.asc? av.localeCompare(bv):bv.localeCompare(av);} av=av||0; bv=bv||0; return repoState.asc? av-bv:bv-av; });
    const tbl=el("table",{class:"wide"}); const hr=el("tr");
    for(const [key,label,cls] of REPO_COLS){ const th=el("th",{class:cls||"",role:"button",tabindex:"0"},label); if(repoState.sort===key) th.classList.add(repoState.asc?"asc":"sorted");
      const doSort=()=>{ if(repoState.sort===key) repoState.asc=!repoState.asc; else {repoState.sort=key; repoState.asc=false;} draw(); };
      th.addEventListener("click",doSort); th.addEventListener("keydown",e=>{ if(e.key==="Enter"||e.key===" "){ e.preventDefault(); doSort(); } }); hr.append(th); }
    hr.append(el("th",{class:"l"},"share"));
    tbl.append(el("thead",null,hr));
    const tb=el("tbody");
    for(const r of rows){ const tr=el("tr");
      for(const [key,label,cls,fn] of REPO_COLS) tr.append(el("td",{class:cls||""}, fn(r)));
      tr.append(el("td",{class:"l"}, barCell(r.commits,maxC)));
      tb.append(tr);
    }
    tbl.append(tb); tblwrap.innerHTML=""; tblwrap.append(tbl);
  }
  draw();
  return sec;
}

function rhythmSection(d){
  const wh=d.by_weekday_hour;
  let max=1; for(const row of wh) for(const v of row) max=Math.max(max,v);
  const cell=15, gap=2, padL=34, padT=16;
  const W=padL+24*(cell+gap), H=padT+7*(cell+gap)+4;
  const s=svg("svg",{viewBox:`0 0 ${W} ${H}`,role:"img","aria-label":"Commits by weekday and hour"});
  const heat=v=>{ if(!v) return "var(--heat0)"; const q=v/max; if(q<=0.15) return "var(--heat1)"; if(q<=0.35) return "var(--heat2)"; if(q<=0.6) return "var(--heat3)"; if(q<=0.85) return "var(--heat4)"; return "var(--heat5)"; };
  for(let wd=0;wd<7;wd++){ const tx=svg("text",{x:0,y:padT+wd*(cell+gap)+cell-3,"font-size":9}); tx.textContent=WD[wd]; s.append(tx);
    for(let h=0;h<24;h++){ const v=wh[wd][h]||0; const x=padL+h*(cell+gap), y=padT+wd*(cell+gap);
      const rr=svg("rect",{x,y,width:cell,height:cell,rx:3,fill:heat(v)});
      rr.addEventListener("mousemove",ev=>showTip(ev,`<b>${WD[wd]} ${String(h).padStart(2,"0")}:00</b><br>${N(v)} commits`)); rr.addEventListener("mouseleave",hideTip);
      s.append(rr);
    }
  }
  for(let h=0;h<24;h+=3){ const tx=svg("text",{x:padL+h*(cell+gap),y:padT-5,"font-size":8}); tx.textContent=String(h); s.append(tx); }
  const totals=wh.map(row=>row.reduce((a,b)=>a+b,0)); const wmax=Math.max(1,...totals);
  const bs=svg("svg",{viewBox:"0 0 760 150",role:"img","aria-label":"Commits per weekday"});
  const bw=760/7;
  totals.forEach((v,wd)=>{ const h=120*v/wmax; const x=bw*wd+bw*0.2; const rr=svg("rect",{x,y:130-h,width:bw*0.6,height:h,fill:"var(--s1)",rx:2});
    rr.addEventListener("mousemove",ev=>showTip(ev,`<b>${WD[wd]}</b><br>${N(v)} commits`)); rr.addEventListener("mouseleave",hideTip); bs.append(rr);
    const tx=svg("text",{x:bw*wd+bw/2,y:146,"text-anchor":"middle","font-size":10}); tx.textContent=WD[wd]; bs.append(tx);
    const vt=svg("text",{x:bw*wd+bw/2,y:126-h,"text-anchor":"middle","font-size":9}); vt.textContent=v?N(v):""; bs.append(vt);
  });
  return card("Rhythm", "commit timing in "+d.meta.timezone,
    el("div",{class:"chart"}, s),
    el("div",{class:"chart"}, bs));
}

function accountsSection(d){
  const personas=(d.by_persona||[]).slice().sort((a,b)=>b.commits-a.commits);
  const max=Math.max(1,...personas.map(p=>p.commits),1);
  const cols=[["Account / login","l"],["Commits",""],["","l"],["PRs op",""],["PRs mg",""],["Iss op",""],["Iss cl",""],["Reviews",""],["Code +/−",""]];
  const tbl=el("table",{class:"wide"}); const hr=el("tr");
  for(const [label,cls] of cols) hr.append(el("th",{class:cls||""},label));
  tbl.append(el("thead",null,hr));
  const tb=el("tbody");
  for(const p of personas){
    const tr=el("tr",{style:"font-weight:600"});
    tr.append(el("td",{class:"l"},p.persona), el("td",null,N(p.commits)), el("td",{class:"l"},barCell(p.commits,max)),
      el("td",null,N(p.prs_opened)), el("td",null,N(p.prs_merged)), el("td",null,N(p.issues_opened)),
      el("td",null,N(p.issues_closed)), el("td",null,N(p.reviews)), el("td",null,pm(p.code_add,p.code_del)));
    tb.append(tr);
    for(const lg of (p.logins||[])){
      const lr=el("tr",{style:"color:var(--muted)"});
      lr.append(el("td",{class:"l"}, el("span",{style:"opacity:.55"},"↳ "), lg.login),
        el("td",null,N(lg.commits)), el("td",{class:"l"},""),
        el("td",null,N(lg.prs_opened)), el("td",null,N(lg.prs_merged)), el("td",null,N(lg.issues_opened)),
        el("td",null,N(lg.issues_closed)), el("td",null,N(lg.reviews)), el("td",null,""));
      tb.append(lr);
    }
  }
  tbl.append(tb);
  const erows=(d.by_identity&&d.by_identity.emails)||[];
  const emax=Math.max(1,...erows.map(x=>x.commits),1);
  const etbl=el("table",{class:"wide"}); const ehr=el("tr");
  for(const [label,cls] of [["Commit email","l"],["Persona","l"],["Commits",""],["","l"]]) ehr.append(el("th",{class:cls||""},label));
  etbl.append(el("thead",null,ehr));
  const etb=el("tbody");
  for(const e of erows) etb.append(el("tr",null, el("td",{class:"l"},e.email), el("td",{class:"l"},e.persona||"—"), el("td",null,N(e.commits)), el("td",{class:"l"},barCell(e.commits,emax))));
  etbl.append(etb);
  const body=[el("div",{class:"tablewrap"},tbl)];
  if(erows.length) body.push(el("h3",{style:"font-size:13px;margin:0;color:var(--ink-2)"},"Per commit email"), el("div",{class:"tablewrap"},etbl));
  const disc=d.meta.discovered_emails||[]; const warns=d.meta.warnings||[];
  if(disc.length){ const c=el("div",{class:"callout"}); c.append(el("strong",null,"Discovered emails ("+disc.length+")"), el("div",null,"Commits by a known login under an email not in config, folded in and written back to config.identities.emails:"));
    const ul=el("div",null); for(const e of disc) ul.append(el("div",null, el("code",null,e.email), " — "+N(e.commits)+" commits"+(e.persona? " ("+e.persona+")":""))); c.append(ul); body.push(c); }
  if(warns.length){ const c=el("div",{class:"callout warn"}); c.append(el("strong",null,"Warnings ("+warns.length+")"));
    const ul=el("ul",{style:"margin:4px 0 0;padding-left:18px"}); for(const w of warns.slice(0,40)) ul.append(el("li",null,w)); if(warns.length>40) ul.append(el("li",null,"…and "+(warns.length-40)+" more")); c.append(ul); body.push(c); }
  return card("Accounts", "one combined scorecard · commits per email, PRs/issues/reviews per login (GitHub-attributed)", ...body);
}

function contextSection(d){
  const dep=(d.context||{}).dependabot;
  if(!dep) return null;
  const c=el("div",{class:"callout context"});
  c.append(el("strong",null,"Dependabot — not your PRs"));
  if(dep.skipped){
    c.append(el("div",null,"Skipped to stay under the search-call budget (would cost "+N(dep.would_cost_search_calls)+" calls)."));
  } else {
    c.append(el("div",{class:"ctx-nums"},
      el("span",null, el("b",null,N(dep.prs_created)), " created"),
      el("span",null, el("b",null,N(dep.prs_merged)), " merged")),
      el("div",{style:"color:var(--muted);font-size:12px"}, "Dependabot's own PRs across "+N(dep.repos_scoped)+" repos with your activity (all groups). Shown for context; excluded from every other number on this page."));
  }
  return card("Context", "dependency-bot activity, for reference · combined across groups", c);
}

function notesSection(d){
  const m=d.meta;
  const scanned = m.repos_scanned_for_commits!=null? m.repos_scanned_for_commits : m.repos_scanned;
  const cov=(m.coverage&&m.coverage.logins_not_logged_in)||[];
  const covText = cov.length
    ? "Logins in your identity list that are not logged in to gh: "+cov.join(", ")+". Their private repos are invisible to this tool. "
    : "Every identity login is logged in to gh. ";
  const items=[
    ["Default branch only","Commits and lines come from git on each repo's default branch. Work on unmerged branches is not counted."],
    ["Merges excluded","git log runs with --no-merges; a squash-merged PR counts as one commit."],
    ["Combined accounts","One scorecard across every account and commit identity you control ("+N((m.identities.personas||[]).length)+" personas, "+m.identities.logins.length+" logins), de-duplicated by commit sha and by PR/issue node id. Per-persona and per-login splits are in Accounts."],
    ["Scope","The Scope control above recomputes every section from one org/owner group (or all groups combined). Streaks, active days, cycle time and ratios are true for the selected scope alone."],
    ["Self-reviews","A review of a PR authored by any of your own logins is not a review given; "+N(d.totals.self_reviews_excluded||0)+" were excluded in this scope."],
    ["Bot commits","Excluded by construction: a bot's email/login is not in the identity list, so a bot commit never matches."],
    ["Identity matching","A commit is “mine” when its author email is in the identity list (case-insensitive, GitHub numeric noreply prefix stripped) or GitHub attributes it to a known login. A login-based presence pass plus a login→email harvest catch commits under a known login whose email was not yet in config."],
    ["Files added / removed","From git log --name-status --diff-filter=AD on the default branch: counts of files added (A) and removed (D), classified code or docs. Paths are never stored."],
    ["PRs closed unmerged","PRs you authored that were closed without merging, counted by close date."],
    ["Releases","Counted by published_at in the year where the release author is one of your logins; drafts excluded. Only repos with your activity are queried; a repo with none is zero, not an error."],
    ["Monthly averages","Divide by elapsed months: January through the current month ("+N((m.through||"").slice(5,7))+" for this year), 12 for a full past year."],
    ["Dependabot context","Dependabot figures are that bot's own PRs across repos with your activity (combined across groups), shown for reference and excluded from all other counts."],
    ["Timezone","Weekday, hour, calendar and streak bucketing use "+m.timezone+". Search date windows are UTC."],
    ["Search API","PRs, issues and reviews come from the GitHub search API per login, split by month to stay under the 1000-result cap, de-duplicated by node id. A PR/issue/review is assigned to a scope by the repo it belongs to."],
    ["Forks","A fork is counted only when an identity has commits in it; forks with no commits are dropped. Fork/private/archived are badged in the repo table."],
    ["Coverage",covText+"Repos discovered: "+N(m.repos_discovered)+"; candidates after pruning: "+N(m.repos_candidates)+"; scanned for commits: "+N(scanned)+"; with activity (all groups): "+N(m.repos_with_activity)+"."]
  ];
  const dl=el("dl",{class:"notes-grid"});
  for(const [k,v] of items) dl.append(el("div",null, el("dt",null,k), el("dd",null,v)));
  return card("How these numbers are counted", "tool v"+m.tool_version+" · "+N(m.search_calls)+" search calls · "+m.runtime_seconds+"s", dl);
}

// ---- boot (hash access guarded for sandboxed iframes) ----
function safeHash(){ try{ return (location.hash||"").replace(/^#/,""); }catch(e){ return ""; } }
function writeHash(y,scope){ try{ location.hash = (scope && scope!=="all")? y+"/"+encodeURIComponent(scope) : String(y); return true; }catch(e){ return false; } }
function fromHash(){
  const h=safeHash(); const parts=h.split("/");
  const y=Number(parts[0]); const year=YEARS.includes(y)? y : Math.max(...YEARS);
  let scope="all"; try{ scope=parts[1]? decodeURIComponent(parts[1]) : "all"; }catch(e){ scope="all"; }
  return {year, scope};
}

addEventListener("mousemove",e=>{ if(tip.style.opacity==="1") moveTip(e); });
const tabs=document.getElementById("tabs");
for(const y of [...YEARS].sort((a,b)=>b-a)){
  const b=el("button",{type:"button",role:"tab","data-y":y,"aria-selected":"false"},String(y));
  b.addEventListener("click",()=>{ writeHash(y, current.scope); render(y, current.scope); });
  tabs.append(b);
}
try{ addEventListener("hashchange",()=>{ const h=fromHash(); if(h.year!==current.year || h.scope!==current.scope) render(h.year, h.scope); }); }catch(e){}
const boot=fromHash();
render(boot.year, boot.scope);
</script>"""


def full_document(payload):
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        + TITLE + "\n" + DESC + "\n" + FONTS + "\n" + STYLE + "\n"
        "</head>\n<body>\n" + BODY.replace("__DATA__", payload) + "\n</body>\n</html>\n"
    )


def fragment(payload):
    # No <!DOCTYPE>/<html>/<head>/<body>: the Artifact publish skeleton supplies
    # those. Order: <title>, fonts, <style>, one outer wrapper, inline <script>.
    return TITLE + "\n" + FONTS + "\n" + STYLE + "\n" + BODY.replace("__DATA__", payload) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=str(HERE / "data"),
                    help="directory of data/<year>.json files to render (default: ./data)")
    ap.add_argument("--out", default=str(HERE / "out" / "velocity.html"),
                    help="standalone HTML output path; the fragment is written alongside as *.artifact.html")
    ap.add_argument("--redact-private", action="store_true",
                    help="replace private repo names with <owner>/private-repo-N so the page can be shared publicly")
    args = ap.parse_args(argv)

    years = load_years(args.data_dir)
    if not years:
        print(f"error: no data/*.json under {args.data_dir}; run collect.py first", file=sys.stderr)
        return 2
    if args.redact_private:
        years = redact_private(years)
    payload = json.dumps(years, separators=(",", ":"), default=str).replace("</", "<\\/")

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = full_document(payload)
    out.write_text(doc)
    frag_path = out.with_name(out.stem + ".artifact.html")
    frag = fragment(payload)
    frag_path.write_text(frag)
    print(f"wrote {out} ({len(doc):,} bytes) and {frag_path} ({len(frag):,} bytes) with years {sorted(years)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
