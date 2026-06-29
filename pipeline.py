#!/usr/bin/env python3
"""
DRIP Season 1 Post-Incentive Retention Dashboard
Generates dashboard.html with all data baked in at build time:
  - TVL data fetched from DeFiLlama and baked as TVL_DATA JSON constant.
  - Unique-wallet data fetched from Dune Analytics and baked as WALLET_DATA.
"""

import json
import os
import time
import datetime
import urllib.request

CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'drip_config.json')
OUTPUT_FILE = os.path.join(os.path.dirname(__file__), 'dashboard.html')


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


# ── DeFiLlama helpers ─────────────────────────────────────────────────────────

def fetch_tvl_data(config):
    """Fetch Arbitrum TVL series for each protocol from DeFiLlama at build time."""
    cutoff = "2025-03-01"
    tvl_data = {}

    for p in config["protocols"]:
        name  = p["name"]
        slug  = p["slug"]
        chain = p.get("chain", "Arbitrum").lower()
        print(f"  {name} ({slug})…")
        try:
            url = f"https://api.llama.fi/protocol/{slug}"
            req = urllib.request.Request(url, headers={"User-Agent": "drip-dashboard/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())

            chain_tvls = data.get("chainTvls", {})
            key = next((k for k in chain_tvls if k.lower() == chain), None)
            raw = chain_tvls[key]["tvl"] if key and chain_tvls[key].get("tvl") else data.get("tvl", [])

            series = []
            for e in raw:
                tvl = e.get("totalLiquidityUSD", 0)
                if tvl <= 0:
                    continue
                date = datetime.datetime.utcfromtimestamp(e["date"]).strftime("%Y-%m-%d")
                if date >= cutoff:
                    series.append({"date": date, "tvl": round(tvl, 2)})

            series.sort(key=lambda x: x["date"])
            tvl_data[name] = series
            print(f"    → {len(series)} data points")
        except Exception as e:
            print(f"    Warning: {e}")

    return tvl_data


# ── Dune Analytics helpers ────────────────────────────────────────────────────

def fetch_dune_results(query_id, api_key, max_wait=120):
    """Return rows from a saved Dune query; execute it first if no cached results."""
    base    = "https://api.dune.com/api/v1"
    headers = {"X-Dune-API-Key": api_key}

    # Try cached results first
    try:
        req = urllib.request.Request(
            f"{base}/query/{query_id}/results", headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        rows = data.get("result", {}).get("rows", [])
        if rows:
            print(f"    (cached: {len(rows)} rows)")
            return rows
    except Exception:
        pass

    # Execute fresh
    req = urllib.request.Request(
        f"{base}/query/{query_id}/execute",
        data=b'{}',
        headers={**headers, "Content-Type": "application/json"},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        exec_id = json.loads(resp.read())["execution_id"]

    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(3)
        req = urllib.request.Request(
            f"{base}/execution/{exec_id}/results", headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        state = result.get("state", "")
        if state == "QUERY_STATE_COMPLETED":
            return result.get("result", {}).get("rows", [])
        if state in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"):
            raise RuntimeError(f"Query {query_id} {state}")
    raise TimeoutError(f"Query {query_id} timed out after {max_wait}s")


def collect_wallet_data(config):
    """Fetch daily unique-wallet counts from Dune for each configured protocol."""
    api_key = config.get("dune_api_key", "")
    if not api_key:
        print("  No dune_api_key in config — skipping wallet data.")
        return {}

    wallet_data = {}
    for p in config["protocols"]:
        qid = p.get("dune_query_id")
        if not qid:
            continue
        print(f"  {p['name']} (query {qid})…")
        try:
            rows = fetch_dune_results(int(qid), api_key)
            series = []
            for r in rows:
                day     = str(r.get("day", r.get("date", "")))[:10]
                wallets = r.get("unique_wallets", r.get("wallets", 0))
                if day and wallets is not None:
                    series.append({"date": day, "wallets": int(wallets)})
            series.sort(key=lambda x: x["date"])
            wallet_data[p["name"]] = series
            print(f"    → {len(series)} data points")
        except Exception as e:
            print(f"    Warning: {e}")

    return wallet_data


# ── Cohort helpers ────────────────────────────────────────────────────────────

CHECKPOINTS = [
    ("Day 1",   "2026-02-02"),
    ("Week 1",  "2026-02-08"),
    ("Month 1", "2026-03-01"),
    ("Month 2", "2026-04-01"),
    ("Month 3", "2026-05-01"),
    ("Month 4", "2026-06-01"),
]


def compute_cohort_from_wallets(protocol_rows_list):
    """Merge per-protocol wallet rows into cross-protocol cohort groups."""
    wallets = {}
    for rows in protocol_rows_list:
        for row in rows:
            w = str(row.get("wallet") or "").lower().strip()
            if not w:
                continue
            if w not in wallets:
                wallets[w] = {"pre": 0, "sup": 0, "last_post": None}
            d = wallets[w]
            d["pre"] = max(d["pre"], int(row.get("pre_season") or 0))
            d["sup"] = max(d["sup"], int(row.get("supplied_s1") or 0))
            lp = row.get("last_post")
            if lp:
                lp_str = str(lp)[:10]
                if d["last_post"] is None or lp_str > d["last_post"]:
                    d["last_post"] = lp_str

    groups = {"group1": [], "group2": [], "group3": []}
    for d in wallets.values():
        grp = "group1" if not d["pre"] else ("group2" if d["sup"] else "group3")
        groups[grp].append(d["last_post"])

    result = {}
    for grp, last_posts in groups.items():
        total = len(last_posts)
        if not total:
            continue
        result[grp] = {
            "total": total,
            "ever_returned": sum(1 for lp in last_posts if lp),
            "checkpoints": [
                {
                    "label":    lbl,
                    "retained": sum(1 for lp in last_posts if lp and lp >= date),
                    "pct":      round(sum(1 for lp in last_posts if lp and lp >= date) / total * 100, 1),
                }
                for lbl, date in CHECKPOINTS
            ],
        }
    return result


def fetch_cohort_data(config):
    """Fetch cohort data — uses per-protocol wallet queries when available, else falls back to legacy cohort query."""
    api_key = config.get("dune_api_key", "")
    if not api_key:
        print("  No dune_api_key in config — skipping cohort analysis.")
        return {}

    wallet_queries = config.get("cohort_wallet_queries", {})
    if wallet_queries:
        print(f"  Cohort analysis — fetching {len(wallet_queries)} per-protocol wallet queries…")
        protocol_rows_list = []
        for proto_name, qid in wallet_queries.items():
            print(f"    [{proto_name}] query {qid}…", end=" ", flush=True)
            try:
                rows = fetch_dune_results(int(qid), api_key)
                print(f"{len(rows)} wallets")
                protocol_rows_list.append(rows)
            except Exception as e:
                print(f"ERROR: {e}")
        result = compute_cohort_from_wallets(protocol_rows_list)
        print(f"    → {len(result)} cohort groups, {sum(v['total'] for v in result.values())} unique wallets across all protocols")
        return result

    # Legacy: single cross-protocol cohort query
    qid = config.get("cohort_query_id")
    if not qid:
        print("  No cohort_query_id or cohort_wallet_queries in config — skipping cohort analysis.")
        return {}
    print(f"  Cohort analysis (legacy query {qid})…")
    try:
        rows = fetch_dune_results(int(qid), api_key)
        checkpoint_cols = [
            ("Day 1",   "ret_day1"),
            ("Week 1",  "ret_week1"),
            ("Month 1", "ret_month1"),
            ("Month 2", "ret_month2"),
            ("Month 3", "ret_month3"),
            ("Month 4", "ret_month4"),
        ]
        result = {}
        for row in rows:
            grp   = row.get("grp", "")
            total = int(row.get("total", 0))
            if not grp or total == 0:
                continue
            result[grp] = {
                "total":         total,
                "ever_returned": int(row.get("ever_returned", 0)),
                "checkpoints": [
                    {
                        "label":    lbl,
                        "retained": int(row.get(col, 0)),
                        "pct":      round(int(row.get(col, 0)) / total * 100, 1),
                    }
                    for lbl, col in checkpoint_cols
                ],
            }
        print(f"    → {len(result)} cohort groups")
        return result
    except Exception as e:
        print(f"    Warning: {e}")
        return {}


# ── HTML generation ───────────────────────────────────────────────────────────

def generate_html(config, wallet_data=None, tvl_data=None, cohort_data=None):
    config_json      = json.dumps(config, separators=(',', ':'))
    wallet_data_json = json.dumps(wallet_data or {}, separators=(',', ':'))
    tvl_data_json    = json.dumps(tvl_data or {}, separators=(',', ':'))
    cohort_data_json = json.dumps(cohort_data or {}, separators=(',', ':'))
    built_at         = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")


    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DRIP Season 1 — Retention Dashboard</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
html{{background:#f4f6f9}}
body{{font-family:"Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  background:transparent;color:#111827;padding:2rem;line-height:1.6;min-height:100vh}}
body::before{{
  content:'';position:fixed;inset:0;z-index:-1;
  background:linear-gradient(rgba(71,85,105,.05) 1px,transparent 1px),linear-gradient(90deg,rgba(71,85,105,.05) 1px,transparent 1px),linear-gradient(135deg,#f9fafb,#f1f5f9,#e2e8f0,#f1f5f9,#f9fafb);
  background-size:20px 20px,20px 20px,400% 400%;
  background-position:0 0,0 0,0% 50%;
  animation:gradientShift 15s ease infinite}}
@keyframes gradientShift{{0%{{background-position:0 0,0 0,0% 50%}}50%{{background-position:0 0,0 0,100% 50%}}100%{{background-position:0 0,0 0,0% 50%}}}}
a{{color:#1d4ed8}}
h1{{font-size:1.6rem;font-weight:700;color:#111827;letter-spacing:-.01em}}
h2{{font-size:.8rem;font-weight:700;color:#374151;margin:2rem 0 .75rem;
  padding-bottom:.5rem;border-bottom:1px solid #e5e7eb;
  text-transform:uppercase;letter-spacing:.07em}}
.subtitle{{color:#6b7280;margin:.35rem 0 1rem;max-width:680px;font-size:.875rem}}
.pills{{display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:1.25rem}}
.pill{{background:#fff;border:1px solid #e5e7eb;border-radius:999px;
  padding:.2rem .75rem;font-size:.75rem;color:#6b7280}}
.pill b{{color:#111827}}
.wrap{{max-width:1120px;margin:0 auto}}

/* card base — solid white */
.glass{{background:#fff;border:1px solid #e5e7eb;border-radius:10px;
  box-shadow:0 1px 3px rgba(0,0,0,.06)}}

/* refresh bar */
.refresh-bar{{display:flex;align-items:center;gap:1rem;margin-bottom:2rem;padding:.6rem 1rem}}
.refresh-bar span{{font-size:.8rem;color:#9ca3af;flex:1}}
.refresh-bar b{{color:#374151}}
.btn{{background:#fff;border:1px solid #e5e7eb;color:#374151;
  font-size:.75rem;padding:.35rem .75rem;border-radius:6px;cursor:pointer;transition:background .15s}}
.btn:hover{{background:#f9fafb}}
.btn:disabled{{opacity:.4;cursor:not-allowed}}

/* loading */
#loading{{text-align:center;padding:4rem;color:#9ca3af}}
.spinner{{width:2rem;height:2rem;border:3px solid #e5e7eb;
  border-top-color:#1d4ed8;border-radius:50%;animation:spin .8s linear infinite;margin:0 auto 1rem}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}

/* stat cards */
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:1rem;margin-bottom:2rem}}
.card{{padding:1.5rem}}
.card .lbl{{font-size:.75rem;color:#6b7280;margin-bottom:.35rem}}
.card .val{{font-size:1.75rem;font-weight:700;color:#111827;letter-spacing:-.02em}}
.card .sub{{font-size:.75rem;color:#9ca3af;margin-top:.25rem}}

/* table */
.tbl-wrap{{overflow:hidden;margin-bottom:2rem}}
table{{width:100%;border-collapse:collapse}}
th{{background:#f9fafb;color:#6b7280;font-size:.7rem;text-transform:uppercase;
  letter-spacing:.06em;padding:.65rem 1rem;text-align:left;font-weight:600;border-bottom:1px solid #e5e7eb}}
td{{padding:.85rem 1rem;border-top:1px solid #f3f4f6;font-size:.875rem;color:#374151}}
tr:hover td{{background:#f9fafb}}

/* charts */
.agg-wrap{{padding:1.25rem;margin-bottom:2rem}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(460px,1fr));
  gap:1rem;margin-bottom:2rem}}
.chart-card{{padding:1.25rem;cursor:pointer;transition:box-shadow .15s}}
.chart-card:hover{{box-shadow:0 4px 12px rgba(0,0,0,.08)}}
.chart-card h3{{font-size:.875rem;font-weight:600;margin-bottom:.75rem;color:#111827;
  display:flex;align-items:center;gap:.5rem}}
.badge{{font-size:.65rem;padding:.2rem .5rem;border-radius:999px;
  color:#fff;font-weight:600}}
.footer{{text-align:center;color:#9ca3af;font-size:.7rem;margin-top:2rem}}
.modal-stat{{background:#f9fafb;border:1px solid #e5e7eb;
  border-radius:8px;padding:.75rem 1rem;min-width:140px}}
.modal-stat-lbl{{font-size:.65rem;text-transform:uppercase;letter-spacing:.06em;color:#6b7280;font-weight:600}}
.modal-stat-val{{font-size:1.3rem;font-weight:700;color:#111827;margin-top:.15rem}}
.modal-stat-sub{{font-size:.65rem;color:#9ca3af;margin-top:.1rem}}

@media(max-width:600px){{
  .grid{{grid-template-columns:1fr}}
  body{{padding:1rem}}
}}
</style>
</head>
<body>
<header style="background:#fff;border-bottom:1px solid #e5e7eb;padding:.75rem 2rem;
  display:flex;align-items:center;justify-content:space-between;
  position:sticky;top:0;z-index:100;margin:-2rem -2rem 2rem -2rem">
  <img src="https://arbdata.com/arb-dao-expanded-dark.svg" alt="Arbitrum DAO" style="height:64px">
  <img src="https://arbdata.com/entropy-lockup-dark.svg" alt="Entropy Advisors" style="height:44px">
</header>
<div class="wrap">
  <h1>DRIP Season 1 — Post-Incentive Retention</h1>
  <p class="subtitle">{config.get("description","")}</p>
  <div class="pills">
    <div class="pill">Season 1: <b>{config["start_date"]} → {config["end_date"]}</b></div>
    <div class="pill">Total ARB deployed: <b>{config.get("total_arb",0)/1e6:.1f}M ARB</b></div>
    <div class="pill">Protocols: <b>{len(config["protocols"])}</b></div>
  </div>

  <div id="loading">
    <div class="spinner"></div>
    <p>Loading…</p>
  </div>

  <div id="content" style="display:none">
    <h2>At a Glance</h2>
    <div class="cards" id="cards"></div>

    <h2>Aggregate TVL — All DRIP Protocols on Arbitrum</h2>
    <div class="agg-wrap glass"><canvas id="agg" height="70"></canvas></div>

    <h2>Stickiness Rankings — Ordered by Post-Incentive Retention</h2>
    <div class="tbl-wrap glass">
      <table>
        <thead><tr>
          <th>Rank</th><th>Protocol</th>
          <th>Baseline TVL</th><th>Peak TVL (during DRIP)</th>
          <th>Current TVL</th><th>Retention vs Pre-DRIP</th><th>Growth (base→peak)</th>
        </tr></thead>
        <tbody id="tbody"></tbody>
      </table>
    </div>

    <h2>TVL History per Protocol &nbsp;<span style="font-weight:400;color:#475569;font-size:.8rem">(purple band = DRIP Season 1 window)</span></h2>
    <div class="grid" id="grid"></div>

    <h2 id="wallet-heading" style="display:none">Unique Daily Active Wallets &nbsp;<span style="font-weight:400;color:#475569;font-size:.8rem">(purple band = DRIP Season 1 window)</span></h2>
    <div class="grid" id="wallet-grid" style="display:none"></div>

    <h2 id="decay-heading" style="display:none">Post-Incentive Decay Model &nbsp;<span style="font-weight:400;color:#475569;font-size:.8rem">fitted to TVL after Season 1 ended (Feb 2026 → today)</span></h2>
    <p id="decay-subtitle" class="subtitle" style="display:none">Each protocol's TVL after incentives ended is fitted to an exponential decay curve <em>TVL(t) = A · e<sup>−λt</sup></em>. Half-life is the number of days for TVL to fall 50% from its post-Season 1 starting value. Dashed lines on charts above show the fitted curves.</p>
    <div class="tbl-wrap glass" id="decay-rank-wrap" style="display:none">
      <table>
        <thead><tr>
          <th>Rank</th><th>Protocol</th><th>Half-Life</th>
          <th>Decay Rate λ</th><th>Verdict</th>
        </tr></thead>
        <tbody id="decay-tbody"></tbody>
      </table>
    </div>
    <div class="agg-wrap glass" id="decay-norm-wrap" style="display:none">
      <p style="font-size:.75rem;color:#64748b;margin-bottom:.75rem">TVL indexed to 100% at Season 1 end (Feb 1 2026) &nbsp;·&nbsp; solid = actual &nbsp;·&nbsp; dashed = fitted decay curve</p>
      <canvas id="decay-norm-chart" height="80"></canvas>
    </div>
  </div>

  <h2 id="cohort-heading" style="display:none">Cohort Retention — Post-Incentive Wallet Behaviour</h2>
    <div id="cohort-subtitle" class="subtitle" style="display:none">
      <p><b>Group 1 (new wallets)</b>: first interaction with any DRIP protocol during Season 1 — no activity in any of the 6 DRIP protocols in the 6 months prior.</p>
      <p><b>Group 2 (existing + added)</b>: had prior activity and made at least one new deposit during Season 1.</p>
      <p><b>Retained at T</b> = wallet had at least one supply or borrow on or after date T.</p>
    </div>
    <div class="cards" id="cohort-cards" style="display:none"></div>
    <div class="agg-wrap glass" id="cohort-chart-wrap" style="display:none">
      <p style="font-size:.75rem;color:#64748b;margin-bottom:.75rem">% of each cohort still active at each checkpoint after Season 1 ended &nbsp;·&nbsp; dashed line = 50% retention threshold</p>
      <canvas id="cohort-chart" height="70"></canvas>
    </div>
    <div class="tbl-wrap glass" id="cohort-tbl-wrap" style="display:none">
      <table>
        <thead><tr>
          <th>Checkpoint</th>
          <th>Group 1 — Retained</th><th>Group 1 — Exited</th>
          <th>Group 2 — Retained</th><th>Group 2 — Exited</th>
        </tr></thead>
        <tbody id="cohort-tbody"></tbody>
      </table>
    </div>

  <div class="footer">
    Data: <a href="https://defillama.com" target="_blank">DeFiLlama API</a> &nbsp;·&nbsp;
    Wallets: <a href="https://dune.com" target="_blank">Dune Analytics</a> &nbsp;·&nbsp;
    Built for <a href="https://forum.arbitrum.foundation/c/dao-grant-programs/entropy-advisors-updates/50" target="_blank">Entropy Advisors</a>
  </div>
</div>

<!-- Fullscreen modal -->
<div id="modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.3);
  z-index:1000;padding:1.5rem;box-sizing:border-box">
  <div class="glass" style="border-radius:12px;
    padding:1.5rem;height:100%;display:flex;flex-direction:column">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem">
      <h2 id="modal-title" style="font-size:1rem;color:#111827;font-weight:600;border:none;margin:0;padding:0;text-transform:none;letter-spacing:normal"></h2>
      <button onclick="closeModal()" class="btn">✕ close</button>
    </div>
    <div id="modal-stats" style="display:flex;gap:.75rem;flex-wrap:wrap;margin-bottom:1rem"></div>
    <div style="flex:1;min-height:0"><canvas id="modal-canvas"></canvas></div>
    <p style="text-align:center;color:#9ca3af;font-size:.7rem;margin-top:.75rem">Click outside or press Esc to close</p>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3.0.1/dist/chartjs-plugin-annotation.min.js"></script>
<script>
const CONFIG      = {config_json};
const WALLET_DATA = {wallet_data_json};
const TVL_DATA    = {tvl_data_json};
const COHORT_DATA = {cohort_data_json};
const BUILT_AT    = "{built_at}";

const SEASON_START = new Date(CONFIG.start_date);
const SEASON_END   = new Date(CONFIG.end_date);
const PRE_START    = new Date(SEASON_START.getTime() - 45 * 864e5);
const CUTOFF       = new Date('2025-03-01');

let aggChart  = null;
let modalChart = null;
let decayNormChart = null;
let protocolCharts = {{}};
let allData = [];
let decayResults = {{}};
let showHoverZone = false;
let currentModalIdx = -1;

// lazy chart rendering
const pendingCharts = new Map();
const chartObserver = new IntersectionObserver(entries => {{
  entries.forEach(el => {{
    if (!el.isIntersecting) return;
    const fn = pendingCharts.get(el.target);
    if (fn) {{ fn(); pendingCharts.delete(el.target); }}
    chartObserver.unobserve(el.target);
  }});
}}, {{ rootMargin: '120px' }});

function lazyChart(canvas, createFn) {{
  pendingCharts.set(canvas, createFn);
  chartObserver.observe(canvas);
}}

// ── helpers ─────────────────────────────────────────────────────────────────
const fmt = v => v >= 1000 ? `$${{(v/1000).toFixed(1)}}B` : v >= 1 ? `$${{v.toFixed(1)}}M` : `$${{(v*1000).toFixed(0)}}K`;
const pct = v => `${{v.toFixed(1)}}%`;

function retentionColor(r) {{
  if (r >= 100) return '#22c55e';
  if (r >= 75)  return '#eab308';
  if (r >= 50)  return '#f97316';
  return '#ef4444';
}}

function computeMetrics(series) {{
  const pre    = series.filter(d => {{ const t = new Date(d.date); return t >= PRE_START && t < SEASON_START; }}).map(d => d.tvl);
  const during = series.filter(d => {{ const t = new Date(d.date); return t >= SEASON_START && t <= SEASON_END; }}).map(d => d.tvl);
  const post   = series.filter(d => new Date(d.date) > SEASON_END).map(d => d.tvl);

  const baseline      = pre.length    ? pre.reduce((a,b)=>a+b,0)/pre.length : 0;
  const preTVL        = pre.length    ? pre[pre.length-1] : 0;
  const peak          = during.length ? Math.max(...during) : 0;
  const current       = post.length   ? post[post.length-1] : 0;
  const retentionVsPre = preTVL > 0  ? (current/preTVL)*100 : 0;

  return {{ baseline, preTVL, peak, current, retentionVsPre, growth: baseline > 0 ? peak/baseline : 0 }};
}}

function windowAnnotation(dates, extra) {{
  const si = dates.findIndex(d => d >= CONFIG.start_date);
  const ei = dates.findIndex(d => d > CONFIG.end_date) - 1;
  const ai = dates.findIndex(d => d >= '2025-04-16');
  const annotations = {{}};

  if (si >= 0) {{
    annotations.box1 = {{
      type:'box', xMin:si, xMax: ei < 0 ? dates.length-1 : ei,
      backgroundColor:'rgba(99,102,241,0.10)',
      borderColor:'rgba(99,102,241,0.45)', borderWidth:1,
      label:{{ display:true, content:'DRIP Season 1', color:'#818cf8',
               position:'start', font:{{size:10}} }}
    }};
  }}

  if (ai >= 0) {{
    annotations.announce = {{
      type:'line', xMin:ai, xMax:ai,
      borderColor:'rgba(251,191,36,0.75)', borderWidth:1.5, borderDash:[5,4],
      label:{{ display:true, content:'Announced', color:'#fbbf24',
               position:'start', font:{{size:9}},
               backgroundColor:'rgba(15,23,42,0.85)', padding:{{x:4,y:2}} }}
    }};
  }}

  if (extra) Object.assign(annotations, extra(dates));
  return annotations;
}}

function baseOpts(dates, annotations) {{
  return {{
    responsive:true, animation:false, maintainAspectRatio:true,
    interaction:{{ mode:'index', intersect:false }},
    plugins:{{
      legend:{{display:false}},
      annotation:{{annotations}},
      tooltip:{{enabled:true}}
    }},
    scales:{{
      x:{{ ticks:{{color:'#9ca3af',maxTicksLimit:8,font:{{size:10}}}},
           grid:{{display:true,color:'rgba(255,255,255,0.06)',lineWidth:1}} }},
      y:{{ ticks:{{color:'#9ca3af',callback:v=>`$${{v}}M`,font:{{size:10}},maxTicksLimit:50}},
           grid:{{display:true,color:'rgba(255,255,255,0.06)',lineWidth:1}} }}
    }}
  }};
}}

// ── hover-zone toggle ────────────────────────────────────────────────────────
function buildDatasets(color, tvls) {{
  const datasets = [];
  if (showHoverZone) {{
    datasets.push({{
      data: tvls, borderColor: color + '44', backgroundColor: 'transparent',
      borderWidth: 20, pointRadius: 0, pointHitRadius: 0,
      fill: false, tension: 0.3, order: 2
    }});
  }}
  datasets.push({{
    data: tvls, borderColor: color, backgroundColor: color + '22',
    borderWidth: 2, pointRadius: 0,
    pointHitRadius: showHoverZone ? 10 : 20,
    pointHoverRadius: 4, fill: true, tension: 0.3, order: 1
  }});
  return datasets;
}}

function zoneInteraction() {{
  return showHoverZone
    ? {{ mode:'nearest', intersect:true }}
    : {{ mode:'index',   intersect:false }};
}}

function toggleZone() {{
  showHoverZone = !showHoverZone;
  const btn = document.getElementById('zone-btn');
  if (showHoverZone) {{
    btn.textContent = '● Hide hover zone';
    btn.style.cssText += ';background:#1e3a2f;border-color:#22c55e;color:#22c55e';
  }} else {{
    btn.textContent = '○ Show hover zone';
    btn.style.cssText = '';
    btn.className = 'btn';
  }}

  allData.forEach((p, i) => {{
    const chart = protocolCharts[i];
    if (!chart) return;
    const tvls = p.series.map(d => +(d.tvl/1e6).toFixed(3));
    chart.data.datasets = buildDatasets(p.color, tvls);
    chart.options.interaction = zoneInteraction();
    chart.options.plugins.tooltip.enabled = showHoverZone;
    chart.update('none');
  }});

  if (aggChart) {{
    aggChart.options.interaction = zoneInteraction();
    aggChart.options.plugins.tooltip.enabled = showHoverZone;
    aggChart.update('none');
  }}

  if (modalChart && currentModalIdx >= 0) {{
    const p = allData[currentModalIdx];
    const tvls = p.series.map(d => +(d.tvl/1e6).toFixed(3));
    modalChart.data.datasets = buildDatasets(p.color, tvls);
    modalChart.options.interaction = zoneInteraction();
    modalChart.options.plugins.tooltip.enabled = showHoverZone;
    modalChart.update('none');
  }}
}}

// ── render ───────────────────────────────────────────────────────────────────
function destroyCharts() {{
  if (aggChart)       {{ aggChart.destroy();       aggChart       = null; }}
  if (decayNormChart) {{ decayNormChart.destroy(); decayNormChart = null; }}
  Object.values(protocolCharts).forEach(c => c.destroy());
  protocolCharts = {{}};
  decayResults   = {{}};
  pendingCharts.forEach((_, canvas) => chartObserver.unobserve(canvas));
  pendingCharts.clear();
  document.getElementById('grid').innerHTML = '';
}}

function render(protocols) {{
  allData = protocols;
  destroyCharts();

  const sorted = [...protocols].sort((a,b) => b.metrics.retentionVsPre - a.metrics.retentionVsPre);

  // ── paint stats + table immediately ─────────────────────────────────────────
  const avgRet  = protocols.reduce((a,p)=>a+p.metrics.retentionVsPre,0)/protocols.length;
  const totPeak = protocols.reduce((a,p)=>a+p.metrics.peak,0);
  const totNow  = protocols.reduce((a,p)=>a+p.metrics.current,0);
  const best    = sorted[0];

  document.getElementById('cards').innerHTML = [
    {{ lbl:'Avg Retention vs Pre-DRIP', val: pct(avgRet),      sub:'across all protocols post-Season 1' }},
    {{ lbl:'Peak Combined TVL',         val: fmt(totPeak/1e6), sub:'during incentive window' }},
    {{ lbl:'Current Combined TVL',      val: fmt(totNow/1e6),  sub:'as of today' }},
    {{ lbl:'Stickiest Protocol',        val: best.name,        sub: pct(best.metrics.retentionVsPre)+' vs pre-DRIP', color: best.color }},
  ].map(c => {{
    const s = c.color ? `style="color:${{c.color}}"` : '';
    return `<div class="card glass"><div class="lbl">${{c.lbl}}</div><div class="val" ${{s}}>${{c.val}}</div><div class="sub">${{c.sub}}</div></div>`;
  }}).join('');

  const medals = ['🥇','🥈','🥉'];
  document.getElementById('tbody').innerHTML = sorted.map((p,i) => {{
    const m = p.metrics;
    return `<tr>
      <td>${{medals[i]||'#'+(i+1)}}</td><td><strong>${{p.name}}</strong></td>
      <td>${{fmt(m.baseline/1e6)}}</td><td>${{fmt(m.peak/1e6)}}</td><td>${{fmt(m.current/1e6)}}</td>
      <td style="color:${{p.color}};font-weight:700">${{pct(m.retentionVsPre)}}</td>
      <td style="color:#60a5fa">${{m.growth.toFixed(1)}}x</td>
    </tr>`;
  }}).join('');

  // pre-compute decay (CPU only, no DOM/canvas)
  decayResults = {{}};
  protocols.forEach(p => {{ const d = fitDecay(p.series); if (d) decayResults[p.name] = d; }});

  // Show content — stats and table are visible before any chart is drawn
  document.getElementById('loading').style.display = 'none';
  document.getElementById('content').style.display = 'block';
  const luEl = document.getElementById('last-updated'); if (luEl) luEl.textContent = BUILT_AT;

  // ── charts in next frame (browser paints content first) ──────────────────
  requestAnimationFrame(() => {{
    // aggregate chart
    const dateMap = {{}};
    protocols.forEach(p => p.series.forEach(d => {{ dateMap[d.date] = (dateMap[d.date]||0) + d.tvl; }}));
    const aggDates = Object.keys(dateMap).sort();
    const aggTvls  = aggDates.map(d => +(dateMap[d]/1e6).toFixed(2));
    aggChart = new Chart(document.getElementById('agg'), {{
      type:'line',
      data:{{ labels:aggDates, datasets:[{{ data:aggTvls,
        borderColor:'#6366f1', backgroundColor:'#6366f133',
        borderWidth:2, pointRadius:0, pointHitRadius:20, pointHoverRadius:4, fill:true, tension:0.3 }}] }},
      options: baseOpts(aggDates, windowAnnotation(aggDates))
    }});

    // protocol charts
    const grid = document.getElementById('grid');
    protocols.forEach((p, i) => {{
      const id = `pc${{i}}`;
      const card = document.createElement('div');
      card.className = 'chart-card glass';
      card.title = 'Click to expand';
      card.innerHTML = `
        <h3>${{p.name}}
          <span class="badge" style="background:${{p.color}}">${{pct(p.metrics.retentionVsPre)}} vs pre-DRIP</span>
          <span style="margin-left:auto;font-size:.7rem;color:#475569;font-weight:400">click to expand ⤢</span>
        </h3>
        <canvas id="${{id}}"></canvas>`;
      card.addEventListener('click', () => openModal(i));
      grid.appendChild(card);

      const dates = p.series.map(d=>d.date);
      const tvls  = p.series.map(d=>+(d.tvl/1e6).toFixed(3));

      const canvas = document.getElementById(id);
      const protocolExtra = p.name === 'Fluid' ? (ds => {{
        const li = ds.findIndex(d => d >= '2026-05-27');
        if (li < 0) return {{}};
        return {{ fluidEvent: {{ type:'line', xMin:li, xMax:li,
          borderColor:'rgba(251,191,36,0.8)', borderWidth:1.5, borderDash:[5,4],
          label:{{ display:true, content:'May 27 Exploit', color:'#fbbf24', position:'end',
                   font:{{size:9}}, backgroundColor:'rgba(15,23,42,0.85)', padding:{{x:4,y:2}} }}
        }} }};
      }}) : null;
      lazyChart(canvas, () => {{
        protocolCharts[i] = new Chart(canvas, {{
          type:'line',
          data:{{ labels:dates, datasets:buildDatasets(p.color, tvls) }},
          options: baseOpts(dates, windowAnnotation(dates, protocolExtra))
        }});
      }});
    }});

    try {{ renderWallets(protocols); }} catch(e) {{ console.error('renderWallets:', e); }}
    try {{ renderDecay(protocols);  }} catch(e) {{ console.error('renderDecay:',  e); }}
    try {{ renderCohort();          }} catch(e) {{ console.error('renderCohort:',  e); }}
  }});
}}

// ── wallet charts ─────────────────────────────────────────────────────────────
function walletChartOpts(dates, protocolName) {{
  const protocolExtra = protocolName === 'Fluid' ? (ds => {{
    const li = ds.findIndex(d => d >= '2026-05-27');
    if (li < 0) return {{}};
    return {{ fluidEvent: {{ type:'line', xMin:li, xMax:li,
      borderColor:'rgba(251,191,36,0.8)', borderWidth:1.5, borderDash:[5,4],
      label:{{ display:true, content:'May 27 Exploit', color:'#fbbf24', position:'end',
               font:{{size:9}}, backgroundColor:'rgba(15,23,42,0.85)', padding:{{x:4,y:2}} }}
    }} }};
  }}) : null;
  const opts = baseOpts(dates, windowAnnotation(dates, protocolExtra));
  opts.scales.y.ticks.callback = v => v >= 1000 ? `${{(v/1000).toFixed(1)}}k` : v;
  opts.plugins.tooltip.enabled = true;
  opts.interaction = {{ mode:'index', intersect:false }};
  return opts;
}}

function openWalletModal(name, series, color) {{
  currentModalIdx = -1;
  document.getElementById('modal-title').textContent = name + ' — Unique Daily Active Wallets (Arbitrum)';
  document.getElementById('modal').style.display = 'block'; document.body.style.overflow = 'hidden';

  const vals   = series.map(d => d.wallets);
  const dates  = series.map(d => d.date);
  const latest = vals[vals.length - 1];
  const peak   = Math.max(...vals);

  const duringVals = series
    .filter(d => d.date >= CONFIG.start_date && d.date <= CONFIG.end_date)
    .map(d => d.wallets);
  const postVals = series
    .filter(d => d.date > CONFIG.end_date)
    .map(d => d.wallets);
  const avgDuring = duringVals.length ? Math.round(duringVals.reduce((a,b)=>a+b,0)/duringVals.length) : null;
  const avgPost   = postVals.length   ? Math.round(postVals.reduce((a,b)=>a+b,0)/postVals.length)   : null;

  document.getElementById('modal-stats').innerHTML = `
    <div class="modal-stat">
      <div class="modal-stat-lbl">Latest Daily Wallets</div>
      <div class="modal-stat-val">${{latest.toLocaleString()}}</div>
    </div>
    <div class="modal-stat">
      <div class="modal-stat-lbl">Peak Daily Wallets</div>
      <div class="modal-stat-val">${{peak.toLocaleString()}}</div>
    </div>
    ${{avgDuring !== null ? `<div class="modal-stat">
      <div class="modal-stat-lbl">Avg During DRIP Season 1</div>
      <div class="modal-stat-val">${{avgDuring.toLocaleString()}}</div>
    </div>` : ''}}
    ${{avgPost !== null ? `<div class="modal-stat">
      <div class="modal-stat-lbl">Avg Post-Season 1</div>
      <div class="modal-stat-val">${{avgPost.toLocaleString()}}</div>
      <div class="modal-stat-sub">vs ${{avgDuring !== null ? avgDuring.toLocaleString() : '—'}} avg during</div>
    </div>` : ''}}
  `;

  if (modalChart) {{ modalChart.destroy(); modalChart = null; }}

  const opts = walletChartOpts(dates, name);
  opts.maintainAspectRatio = false;

  modalChart = new Chart(document.getElementById('modal-canvas'), {{
    type:'line',
    data:{{ labels:dates, datasets:[{{
      data:vals, borderColor:color, backgroundColor:color+'22',
      borderWidth:2, pointRadius:0, pointHoverRadius:4, fill:true, tension:0.3
    }}] }},
    options: opts
  }});
}}

function renderWallets(protocols) {{
  const heading = document.getElementById('wallet-heading');
  const grid    = document.getElementById('wallet-grid');
  grid.innerHTML = '';
  let hasAny = false;

  protocols.forEach((p, i) => {{
    const series = WALLET_DATA[p.name];
    if (!series || !series.length) return;
    hasAny = true;

    const id     = `wc${{i}}`;
    const latest = series[series.length-1].wallets;
    const card   = document.createElement('div');
    card.className = 'chart-card glass';
    card.title = 'Click to expand';
    card.innerHTML = `
      <h3>${{p.name}}
        <span class="badge" style="background:${{p.color}}">${{latest.toLocaleString()}} wallets (latest)</span>
        <span style="margin-left:auto;font-size:.7rem;color:#475569;font-weight:400">click to expand ⤢</span>
      </h3>
      <canvas id="${{id}}"></canvas>`;
    card.addEventListener('click', () => openWalletModal(p.name, series, p.color));
    grid.appendChild(card);

    const dates = series.map(d => d.date);
    const vals  = series.map(d => d.wallets);
    const wCanvas = document.getElementById(id);
    lazyChart(wCanvas, () => {{
      new Chart(wCanvas, {{
        type:'line',
        data:{{ labels:dates, datasets:[{{
          data:vals, borderColor:p.color, backgroundColor:p.color+'22',
          borderWidth:2, pointRadius:0, pointHoverRadius:4, fill:true, tension:0.3
        }}] }},
        options: walletChartOpts(dates, p.name)
      }});
    }});
  }});

  if (hasAny) {{
    heading.style.display = '';
    grid.style.display    = '';
  }}
}}

// ── decay model ──────────────────────────────────────────────────────────────
function fitDecay(series) {{
  const postDRIP = series.filter(d => d.date > CONFIG.end_date);
  if (postDRIP.length < 14) return null;
  const t0  = new Date(CONFIG.end_date).getTime();
  const pts = postDRIP
    .map(d => ({{ t: (new Date(d.date).getTime() - t0) / 864e5, y: d.tvl }}))
    .filter(p => p.y > 0);
  if (pts.length < 14) return null;

  const lnY     = pts.map(p => Math.log(p.y));
  const n       = pts.length;
  const sumT    = pts.reduce((s,p) => s + p.t, 0);
  const sumLnY  = lnY.reduce((s,v) => s + v, 0);
  const sumT2   = pts.reduce((s,p) => s + p.t*p.t, 0);
  const sumTLnY = pts.reduce((s,p,i) => s + p.t*lnY[i], 0);
  const denom   = n*sumT2 - sumT*sumT;
  if (Math.abs(denom) < 1e-10) return null;

  const b      = (n*sumTLnY - sumT*sumLnY) / denom;
  const a      = (sumLnY - b*sumT) / n;
  const lambda = -b;
  const A      = Math.exp(a);
  const halfLife = lambda > 0 ? Math.log(2)/lambda : null;

  const meanLnY = sumLnY / n;
  const ssTot   = lnY.reduce((s,v) => s + (v-meanLnY)**2, 0);
  const ssRes   = pts.reduce((s,p,i) => s + (lnY[i] - (a+b*p.t))**2, 0);
  const r2      = ssTot > 0 ? Math.max(0, 1 - ssRes/ssTot) : 0;

  const fittedByDate = {{}};
  postDRIP.forEach(d => {{
    const t = (new Date(d.date).getTime() - t0) / 864e5;
    fittedByDate[d.date] = A * Math.exp(-lambda * t);
  }});
  return {{ lambda, A, halfLife, r2, fittedByDate }};
}}

function halfLifeColor(hl) {{
  if (hl === null || hl <= 0) return '#22c55e';
  if (hl > 180) return '#22c55e';
  if (hl > 90)  return '#84cc16';
  if (hl > 45)  return '#eab308';
  return '#ef4444';
}}

function halfLifeVerdict(hl) {{
  if (hl === null || hl <= 0) return 'Growing ↑';
  if (hl > 180) return 'Very sticky';
  if (hl > 90)  return 'Sticky';
  if (hl > 45)  return 'Moderate decay';
  return 'High decay';
}}

function renderDecay(protocols) {{
  if (decayNormChart) {{ decayNormChart.destroy(); decayNormChart = null; }}

  // decayResults already populated in render() before charts were created
  const ranked = protocols
    .filter(p => decayResults[p.name])
    .map(p => ({{ ...p, decay: decayResults[p.name] }}));

  if (!ranked.length) return;

  // Sort longest half-life first
  ranked.sort((a, b) => {{
    const ha = a.decay.halfLife === null ? Infinity : a.decay.halfLife;
    const hb = b.decay.halfLife === null ? Infinity : b.decay.halfLife;
    return hb - ha;
  }});

  document.getElementById('decay-tbody').innerHTML = ranked.map((p, rank) => {{
    const d   = p.decay;
    const hl  = d.halfLife;
    const c   = halfLifeColor(hl);
    const hlStr = hl === null ? '∞ (growing)' : Math.round(hl) + ' days';
    return `<tr>
      <td>#${{rank+1}}</td>
      <td><strong style="color:${{p.color}}">${{p.name}}</strong></td>
      <td style="color:${{c}};font-weight:700">${{hlStr}}</td>
      <td style="font-family:monospace;font-size:.8rem">${{(d.lambda*100).toFixed(3)}}%/day</td>
      <td style="color:${{c}}">${{halfLifeVerdict(hl)}}</td>
    </tr>`;
  }}).join('');

  // Normalized post-DRIP chart
  const postDates = [...new Set(
    protocols.flatMap(p => p.series.filter(d => d.date >= CONFIG.end_date).map(d => d.date))
  )].sort();

  const normDatasets = [];
  protocols.forEach(p => {{
    const d = decayResults[p.name];
    if (!d) return;
    const tvlMap  = new Map(p.series.map(s => [s.date, s.tvl]));
    const baseline = p.series.find(s => s.date >= CONFIG.end_date);
    if (!baseline || !baseline.tvl) return;
    const base = baseline.tvl;

    normDatasets.push({{
      label: p.name,
      data: postDates.map(date => {{
        const tvl = tvlMap.get(date);
        return tvl !== undefined ? +(tvl/base*100).toFixed(2) : NaN;
      }}),
      borderColor: p.color, backgroundColor: 'transparent',
      borderWidth: 2, pointRadius: 0, fill: false, tension: 0.3
    }});
    normDatasets.push({{
      label: p.name + ' fit',
      data: postDates.map(date => {{
        const v = d.fittedByDate[date];
        return v !== undefined ? +(v/base*100).toFixed(2) : NaN;
      }}),
      borderColor: p.color + '88', backgroundColor: 'transparent',
      borderWidth: 1.5, borderDash: [5, 3], pointRadius: 0, fill: false, tension: 0
    }});
  }});

  decayNormChart = new Chart(document.getElementById('decay-norm-chart'), {{
      type: 'line',
      data: {{ labels: postDates, datasets: normDatasets }},
      options: {{
        responsive: true, animation: false, maintainAspectRatio: true,
        interaction: {{ mode: 'index', intersect: false }},
        plugins: {{
          legend: {{ display: true, position: 'top',
            labels: {{ color: '#374151', font: {{size:10}},
              filter: item => !item.label?.endsWith(' fit') }} }},
          annotation: {{ annotations: {{
            baseline: {{ type:'line', yMin:100, yMax:100,
              borderColor:'#d1d5db', borderWidth:1, borderDash:[3,3] }}
          }} }},
          tooltip: {{
            enabled: true,
            filter: item => !item.dataset.label.endsWith(' fit'),
            callbacks: {{ label: ctx => `${{ctx.dataset.label}}: ${{ctx.parsed.y?.toFixed(1)}}%` }}
          }}
        }},
        scales: {{
          x: {{ ticks:{{color:'#9ca3af',maxTicksLimit:8,font:{{size:10}}}},
                 grid:{{color:'#f3f4f6',lineWidth:1}} }},
          y: {{ ticks:{{color:'#9ca3af',callback:v=>v+'%',font:{{size:10}}}},
                 grid:{{color:'#f3f4f6',lineWidth:1}} }}
        }}
      }}
    }});

  document.getElementById('decay-heading').style.display  = '';
  document.getElementById('decay-subtitle').style.display = '';
  document.getElementById('decay-rank-wrap').style.display = '';
  document.getElementById('decay-norm-wrap').style.display = '';
}}

// ── cohort retention ─────────────────────────────────────────────────────────
function renderCohort() {{
  const g1 = COHORT_DATA['group1'];
  const g2 = COHORT_DATA['group2'];
  const g3 = COHORT_DATA['group3'];
  if (!g1 && !g2) return;

  const cards = [];
  if (g1) cards.push(`<div class="card glass"><div class="lbl">Group 1 — New Wallets</div><div class="val">${{g1.total.toLocaleString()}}</div><div class="sub">No prior 6-mo activity · ${{g1.ever_returned.toLocaleString()}} ever returned post-season</div></div>`);
  if (g2) cards.push(`<div class="card glass"><div class="lbl">Group 2 — Existing + Added Funds</div><div class="val">${{g2.total.toLocaleString()}}</div><div class="sub">Pre-DRIP users who deposited more · ${{g2.ever_returned.toLocaleString()}} returned post-season</div></div>`);
  if (g3) cards.push(`<div class="card glass"><div class="lbl">Group 3 — Existing, No New Deposits</div><div class="val">${{g3.total.toLocaleString()}}</div><div class="sub">Pre-DRIP users without new supply events</div></div>`);
  document.getElementById('cohort-cards').innerHTML = cards.join('');

  const labels = ['Season End', 'Day 1', 'Week 1', 'Month 1', 'Month 2', 'Month 3', 'Month 4'];
  const datasets = [];
  const mkDs = (grp, label, color) => {{
    if (!grp) return;
    datasets.push({{
      label, borderColor: color, backgroundColor: color + '22',
      data: [100, ...grp.checkpoints.map(c => c.pct)],
      borderWidth: 2, pointRadius: 4, pointHoverRadius: 6, fill: false, tension: 0.3
    }});
  }};
  mkDs(g1, 'Group 1: New Wallets', '#818cf8');
  mkDs(g2, 'Group 2: Existing + Added', '#34d399');

  // Make section visible before chart creation so it shows even if chart errors
  ['cohort-heading','cohort-subtitle','cohort-cards','cohort-chart-wrap','cohort-tbl-wrap']
    .forEach(id => {{ document.getElementById(id).style.display = ''; }});

  new Chart(document.getElementById('cohort-chart'), {{
    type: 'line',
    data: {{ labels, datasets }},
    options: {{
      responsive: true, animation: false, maintainAspectRatio: true,
      interaction: {{ mode: 'index', intersect: false }},
      plugins: {{
        legend: {{ display: true, position: 'top', labels: {{ color: '#374151', font: {{size:11}} }} }},
        annotation: {{ annotations: {{
          half: {{ type:'line', yMin:50, yMax:50,
            borderColor:'#d1d5db', borderWidth:1, borderDash:[4,4],
            label:{{ display:true, content:'50%', color:'#6b7280', position:'end', font:{{size:9}},
              backgroundColor:'#fff', padding:{{x:4,y:2}} }} }}
        }} }},
        tooltip: {{ enabled:true, callbacks:{{ label: ctx => `${{ctx.dataset.label}}: ${{ctx.parsed.y?.toFixed(1)}}% retained` }} }}
      }},
      scales: {{
        x: {{ ticks:{{color:'#9ca3af',font:{{size:11}}}}, grid:{{color:'#f3f4f6',lineWidth:1}} }},
        y: {{ min:0, max:100,
          ticks:{{color:'#9ca3af',callback:v=>v+'%',font:{{size:11}}}},
          grid:{{color:'#f3f4f6',lineWidth:1}} }}
      }}
    }}
  }});

  const bar = (pct, color) => `<div style="display:flex;align-items:center;gap:.5rem"><div style="flex:1;background:rgba(255,255,255,0.07);border-radius:3px;height:12px;overflow:hidden"><div style="width:${{Math.min(pct,100)}}%;height:100%;background:${{color}};border-radius:3px"></div></div><span style="font-size:.75rem;color:${{color}};min-width:3.5rem;text-align:right;font-variant-numeric:tabular-nums">${{pct.toFixed(1)}}%</span></div>`;
  const anchor = g1 || g2;
  const rows = anchor.checkpoints.map((_, i) => {{
    const fmt = (g) => {{
      if (!g) return ['—','—'];
      const c = g.checkpoints[i];
      const exitPct = parseFloat((100-c.pct).toFixed(1));
      return [bar(c.pct,'#818cf8'), bar(exitPct,'#ef4444')];
    }};
    const [g1r, g1e] = fmt(g1);
    const [g2r, g2e] = fmt(g2);
    const lbl = anchor.checkpoints[i].label;
    return `<tr><td>${{lbl}}</td><td>${{g1r}}</td><td>${{g1e}}</td><td>${{g2r}}</td><td>${{g2e}}</td></tr>`;
  }});
  document.getElementById('cohort-tbody').innerHTML = rows.join('');
}}

// ── main load ────────────────────────────────────────────────────────────────
function loadData() {{
  const results = CONFIG.protocols.map(p => {{
    const series  = (TVL_DATA[p.name] || []).filter(d => d.date >= '2025-03-01');
    const metrics = computeMetrics(series);
    const color   = retentionColor(metrics.retentionVsPre);
    return {{ name:p.name, slug:p.slug, color, metrics, series }};
  }}).filter(p => p.series.length > 0);
  render(results);
}}

// ── modal ────────────────────────────────────────────────────────────────────
function openModal(i) {{
  currentModalIdx = i;
  const p = allData[i];
  const m = p.metrics;

  document.getElementById('modal-title').textContent = p.name + ' — TVL History (Arbitrum)';
  document.getElementById('modal').style.display = 'block'; document.body.style.overflow = 'hidden';

  const preColor = retentionColor(m.retentionVsPre);
  const decay    = decayResults[p.name];
  const hlColor  = decay ? halfLifeColor(decay.halfLife) : '#64748b';
  const hlStr    = decay ? (decay.halfLife === null ? '∞' : Math.round(decay.halfLife) + 'd') : '—';
  document.getElementById('modal-stats').innerHTML = `
    <div class="modal-stat">
      <div class="modal-stat-lbl">Current TVL (24h)</div>
      <div class="modal-stat-val">${{fmt(m.current/1e6)}}</div>
    </div>
    <div class="modal-stat">
      <div class="modal-stat-lbl">Peak TVL (Season 1)</div>
      <div class="modal-stat-val">${{fmt(m.peak/1e6)}}</div>
    </div>
    <div class="modal-stat" style="border-color:${{preColor}}40">
      <div class="modal-stat-lbl">Retention vs Pre-Season TVL</div>
      <div class="modal-stat-val" style="color:${{preColor}}">${{pct(m.retentionVsPre)}}</div>
      <div class="modal-stat-sub">current ÷ TVL before DRIP started</div>
    </div>
    <div class="modal-stat" style="border-color:${{hlColor}}40">
      <div class="modal-stat-lbl">TVL Half-Life (post-Season 1)</div>
      <div class="modal-stat-val" style="color:${{hlColor}}">${{hlStr}}</div>
      <div class="modal-stat-sub">${{decay ? halfLifeVerdict(decay.halfLife) : 'no data'}}</div>
    </div>
  `;

  if (modalChart) {{ modalChart.destroy(); modalChart = null; }}

  const dates = p.series.map(d=>d.date);
  const tvls  = p.series.map(d=>+(d.tvl/1e6).toFixed(3));
  const opts  = baseOpts(dates, windowAnnotation(dates));
  opts.maintainAspectRatio = false;

  modalChart = new Chart(document.getElementById('modal-canvas'), {{
    type:'line',
    data:{{ labels:dates, datasets:buildDatasets(p.color, tvls) }},
    options: opts
  }});
}}

function closeModal() {{
  document.getElementById('modal').style.display = 'none';
  document.body.style.overflow = '';
  if (modalChart) {{ modalChart.destroy(); modalChart = null; }}
  currentModalIdx = -1;
}}

document.getElementById('modal').addEventListener('click', e => {{ if (e.target === document.getElementById('modal')) closeModal(); }});
document.addEventListener('keydown', e => {{ if (e.key === 'Escape') closeModal(); }});

// kick off
loadData();
</script>
</body>
</html>'''


def main():
    config = load_config()
    print("Fetching TVL data from DeFiLlama…")
    tvl_data = fetch_tvl_data(config)
    print("Fetching wallet data from Dune…")
    wallet_data = collect_wallet_data(config)
    print("Fetching cohort data from Dune…")
    cohort_data = fetch_cohort_data(config)
    html = generate_html(config, wallet_data, tvl_data, cohort_data)
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"Dashboard saved → dashboard.html")
    print(f"Open: file://{os.path.abspath(OUTPUT_FILE)}")


if __name__ == '__main__':
    main()
