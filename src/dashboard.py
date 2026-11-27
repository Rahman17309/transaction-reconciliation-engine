"""Build the month-end reconciliation dashboard (one self-contained HTML file).

    python src/dashboard.py                 -> dashboard/reconciliation_dashboard.html
                                               data/output/dashboard/*.csv  (the marts, Power BI-ready)
    python src/dashboard.py --plotly-src ../vendor/plotly-2.35.2.min.js --out <path>
                                            -> same page, loading plotly.js from a local copy

Four report pages, laid out like a Power BI report:
  1 Overview      KPI cards, cash bridge, daily leg-1 status, exceptions by type
  2 Exceptions    slicers (owner, class, type, age, search) over the 619-item register, ageing matrix,
                  Pareto of break value, filtered table with CSV export
  3 Gateway fees  charged vs contracted MDR per method per day, cumulative overcharge
  4 Settlements   payout tracker (credited / overdue / in transit), break rate by payment method

Every number comes from dashboard/marts.sql on top of the engine tables. Before writing anything
the script checks that each mart ties back to the engine outputs and stops if one does not.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

sys.path.insert(0, str(Path(__file__).resolve().parent))
import match  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MARTS_SQL = ROOT / "dashboard" / "marts.sql"
OUT_HTML = ROOT / "dashboard" / "reconciliation_dashboard.html"
OUT_MARTS = ROOT / "data" / "output" / "dashboard"
PLOTLY_CDN = "https://cdn.plot.ly/plotly-2.35.2.min.js"

C = {"matched": "#2a9d8f", "timing": "#e9b949", "break": "#d1495b", "claim": "#6a4c93",
     "ink": "#1f2937", "muted": "#6b7280", "grid": "#e5e7eb", "start": "#264653"}
AGE_ORDER = ["not yet due", "0-7 d", "8-14 d", "15-21 d", "22+ d"]
TYPE_LABEL = {
    "SETTLEMENT_OVERDUE": "Settlement overdue", "NO_GATEWAY_RECORD": "Booked, no gateway record",
    "REFUND_NO_CREDIT_NOTE": "Refund with no credit note", "GATEWAY_NOT_BOOKED": "Captured, not booked",
    "BOOKED_PAYMENT_FAILED": "Booked, payment failed", "CHARGEBACK_NOT_BOOKED": "Chargeback not booked",
    "AMOUNT_MISMATCH": "Amount mismatch (discount)", "FEE_ABOVE_CONTRACT": "Fee above contract",
    "SETTLEMENT_IN_TRANSIT": "Settlement in transit", "CUTOFF_TIMING": "Cut-off window",
    "REFUND_CN_PENDING": "Refund, credit note pending", "REFUND_CN_AMOUNT_DIFFERS": "Refund ≠ credit note",
}


# ---------------------------------------------------------------- formatting
def indian(x: float, dp: int = 2) -> str:
    """12345678.9 -> '1,23,45,678.90' (Indian digit grouping)."""
    neg, x = x < 0, abs(x)
    whole, frac = f"{x:.{dp}f}".split(".") if dp else (f"{x:.0f}", "")
    head, tail = whole[:-3], whole[-3:]
    while len(head) > 2:
        tail = head[-2:] + "," + tail
        head = head[:-2]
    s = (head + "," + tail) if head else tail
    return ("−" if neg else "") + s + (("." + frac) if dp else "")


def lakh(x: float) -> str:
    sign, x = ("−" if x < 0 else ""), abs(x)
    if x >= 1e7:
        return f"{sign}₹{x / 1e7:.2f} Cr"
    if x >= 1e5:
        return f"{sign}₹{x / 1e5:.2f} L"
    return sign + "₹" + indian(x, 0)


def dmy(d) -> str:
    d = pd.Timestamp(d)
    return f"{d.day} {d:%b}"


def items(n: int) -> str:
    return f"{n} item" + ("" if n == 1 else "s")


# ---------------------------------------------------------------- data
def load() -> tuple[dict[str, pd.DataFrame], list[tuple[str, bool]]]:
    con = match.build()
    con.execute(MARTS_SQL.read_text())
    names = ["dash_kpis", "dash_daily_status", "dash_owner_queue", "dash_fee_daily",
             "dash_settlements", "dash_method_breaks"]
    m = {n: con.sql(f"SELECT * FROM {n}").df() for n in names}
    m["register"] = con.sql("SELECT * FROM exception_register ORDER BY exception_id").df()
    m["bridge"] = con.sql("SELECT * FROM cash_bridge ORDER BY step").df()
    m["summary"] = con.sql("SELECT * FROM exception_summary").df()
    q = lambda s: con.execute(s).fetchall()[0][0]  # noqa: E731

    k = m["dash_kpis"].iloc[0]
    ties = [
        ("daily status keys = leg-1 keys (excl. no-action)",
         int(m["dash_daily_status"]["keys"].sum()) ==
         q("SELECT COUNT(*) FROM leg1_match JOIN rule_catalogue USING (rule) WHERE outcome <> 'no_action'")),
        ("daily break value = leg-1 break value",
         round(m["dash_daily_status"]["break_inr"].sum(), 2) ==
         round(q("SELECT SUM(value_inr) FROM leg1_match JOIN rule_catalogue USING (rule) WHERE outcome = 'break'"), 2)),
        ("owner queue items and ₹ = exception register",
         int(m["dash_owner_queue"]["items"].sum()) == len(m["register"])
         and round(m["dash_owner_queue"]["impact_inr"].sum(), 2) == round(m["register"]["impact_inr"].sum(), 2)),
        ("fee excess = fee claims in the register",
         round(m["dash_fee_daily"]["excess_incl_gst_inr"].sum(), 2) ==
         round(m["register"].query("`class` == 'claim'")["impact_inr"].sum(), 2)),
        ("settlement tracker covers every payout once",
         len(m["dash_settlements"]) == q("SELECT COUNT(*) FROM payouts")
         and m["dash_settlements"]["settlement_id"].is_unique),
        ("bridge start − bank credits = sum of bridge steps",
         round(k.erp_net_sales_inr + m["bridge"].query("kind not in ('start','end')")["amount_inr"].sum(), 2)
         == round(k.bank_credits_inr, 2)),
        ("unexplained residual is ₹0.00", abs(k.unexplained_inr) < 0.005),
    ]
    con.close()
    return m, ties


# ---------------------------------------------------------------- figures
def _layout(fig: go.Figure, title: str, h: int = 380, legend_below: bool = False, **kw) -> go.Figure:
    kw.setdefault("margin", dict(l=60, r=24, t=56, b=90 if legend_below else 48))
    legend = (dict(orientation="h", y=-0.14, yanchor="top", x=0.5, xanchor="center") if legend_below
              else dict(orientation="h", y=1.02, yanchor="bottom", x=1, xanchor="right"))
    fig.update_layout(title=dict(text=title, x=0.01, font=dict(size=15, color=C["ink"])),
                      height=h, paper_bgcolor="white",
                      plot_bgcolor="white", font=dict(family="Segoe UI, Inter, Arial", size=12, color=C["ink"]),
                      legend=legend,
                      hoverlabel=dict(bgcolor="white"), **kw)
    fig.update_xaxes(showgrid=False, linecolor=C["grid"])
    fig.update_yaxes(gridcolor=C["grid"], zeroline=False)
    return fig


def fig_bridge(b: pd.DataFrame) -> go.Figure:
    steps = b[b.kind != "residual"].copy()
    measure = ["absolute" if k == "start" else "total" if k == "end" else "relative" for k in steps.kind]
    short = {
        1: "ERP net sales", 2: "Booked, not captured", 3: "Captured, not booked", 4: "Checkout discounts",
        5: "Rounding", 6: "Cut-off", 7: "Fees + GST", 8: "Refunds, no CN", 9: "Refund timing",
        10: "Chargebacks", 11: "30 Sep batch in", 12: "19 Oct batch held", 13: "31 Oct in transit",
        15: "Bank credits"}
    labels = [short.get(s, i) for s, i in zip(steps.step, steps["item"])]
    fig = go.Figure(go.Waterfall(
        x=labels, y=steps.amount_inr / 1e5, measure=measure,
        text=[lakh(v) for v in steps.amount_inr], textposition="outside", cliponaxis=False,
        customdata=steps["item"], hovertemplate="%{customdata}<br>₹%{y:.2f} L<extra></extra>",
        increasing=dict(marker=dict(color=C["matched"])), decreasing=dict(marker=dict(color=C["break"])),
        totals=dict(marker=dict(color=C["start"])), connector=dict(line=dict(color=C["grid"]))))
    lo = (b.running_inr.min() / 1e5) * 0.97
    fig.update_yaxes(range=[lo, b.running_inr.max() / 1e5 * 1.01], title="₹ lakh")
    return _layout(fig, "Cash bridge: ERP net sales → bank credits (unexplained ₹0.00)", 430, showlegend=False)


def fig_daily(d: pd.DataFrame) -> go.Figure:
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    for col, name, color in [("matched_keys", "Auto-matched", C["matched"]),
                             ("timing_keys", "Timing", C["timing"]), ("break_keys", "Break", C["break"])]:
        fig.add_bar(x=d.day_ist, y=d[col], name=name, marker_color=color,
                    hovertemplate=f"{name}: %{{y:,}} keys<extra></extra>")
    fig.add_scatter(x=d.day_ist, y=d.break_rate_7d * 100, name="Break rate, 7-day (rhs)", mode="lines",
                    line=dict(color=C["ink"], width=2), secondary_y=True,
                    hovertemplate="%{y:.2f}%<extra>7-day break rate</extra>")
    fig.update_layout(barmode="stack")
    fig.update_yaxes(title_text="order keys", secondary_y=False)
    fig.update_yaxes(title_text="break rate %", secondary_y=True, showgrid=False, rangemode="tozero")
    return _layout(fig, "Ledger ↔ gateway, by IST day: outcome of every order key", 400, legend_below=True,
                   hovermode="x unified")


def fig_types(s: pd.DataFrame) -> go.Figure:
    s = s.assign(label=s.exception_type.map(TYPE_LABEL)).sort_values("impact_inr")
    fig = go.Figure(go.Bar(
        y=s.label, x=s.impact_inr / 1e5, orientation="h", marker_color=[C[c] for c in s["class"]],
        text=[f"{lakh(v)} · {items(n)}" for v, n in zip(s.impact_inr, s["items"])], textposition="outside",
        cliponaxis=False, customdata=s[["class", "owner"]],
        hovertemplate="%{y}<br>%{customdata[0]} · %{customdata[1]}<br>₹%{x:.2f} L<extra></extra>"))
    fig.update_xaxes(title="₹ lakh", range=[0, s.impact_inr.max() / 1e5 * 1.35])
    return _layout(fig, "Open items by type (red break · amber timing · purple claim)", 430,
                   showlegend=False, margin=dict(l=200, r=24, t=56, b=48))


def fig_ageing(q: pd.DataFrame) -> go.Figure:
    pv = (q.groupby(["owner", "age_bucket"]).items.sum().unstack().reindex(columns=AGE_ORDER).fillna(0))
    val = (q.groupby(["owner", "age_bucket"]).impact_inr.sum().unstack().reindex(columns=AGE_ORDER).fillna(0))
    text = [[f"{int(n)}<br>{lakh(v)}" if n else "" for n, v in zip(rn, rv)] for rn, rv in zip(pv.values, val.values)]
    fig = go.Figure(go.Heatmap(z=pv.values, x=AGE_ORDER, y=pv.index, text=text, texttemplate="%{text}",
                               colorscale=[[0, "#ffffff"], [1, C["break"]]], showscale=False,
                               hovertemplate="%{y} · %{x}: %{z} items<extra></extra>"))
    fig.update_yaxes(autorange="reversed")
    return _layout(fig, "Ageing at close: items (and ₹) per owner", 330, margin=dict(l=190, r=24, t=56, b=40))


def fig_pareto(r: pd.DataFrame) -> go.Figure:
    b = r[r["class"] == "break"].sort_values("impact_inr", ascending=False).reset_index(drop=True)
    b["rank"] = b.index + 1
    cum = b.impact_inr.cumsum() / b.impact_inr.sum() * 100
    n80 = int((cum < 80).sum()) + 1
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_bar(x=b["rank"], y=b.impact_inr, name="Item ₹", marker_color=C["break"],
                customdata=b[["ref", "exception_type"]],
                hovertemplate="#%{x} %{customdata[0]}<br>%{customdata[1]}<br>₹%{y:,.0f}<extra></extra>")
    fig.add_scatter(x=b["rank"], y=cum, name="Cumulative % (rhs)", line=dict(color=C["ink"], width=2),
                    secondary_y=True, hovertemplate="top %{x}: %{y:.1f}%<extra></extra>")
    fig.add_vline(x=n80, line_dash="dot", line_color=C["muted"],
                  annotation_text=f"{n80} items = 80% of break ₹", annotation_position="top right")
    fig.update_yaxes(type="log", title_text="₹ per item (log)", secondary_y=False)
    fig.update_yaxes(range=[0, 101], title_text="cumulative %", secondary_y=True, showgrid=False)
    fig.update_xaxes(title="break items, largest first")
    return _layout(fig, f"Pareto of {len(b)} breaks: work the top of the list first", 340)


def fig_fees(f: pd.DataFrame) -> go.Figure:
    fig = make_subplots(rows=1, cols=4, subplot_titles=["UPI", "Card", "NetBanking", "Wallet"],
                        horizontal_spacing=0.06)
    for i, meth in enumerate(["UPI", "Card", "NetBanking", "Wallet"], start=1):
        d = f[f.method == meth]
        fig.add_scatter(x=d.day_ist, y=d.charged_rate * 100, mode="lines+markers", marker=dict(size=4),
                        line=dict(color=C["break"]), name="Charged", showlegend=i == 1, row=1, col=i,
                        hovertemplate="%{x|%d %b}: %{y:.3f}%<extra>charged</extra>")
        fig.add_scatter(x=d.day_ist, y=d.contract_rate * 100, mode="lines", line=dict(color=C["matched"], dash="dash"),
                        name="Contract", showlegend=i == 1, row=1, col=i,
                        hovertemplate="%{y:.3f}%<extra>contract</extra>")
        c = d.contract_rate.iloc[0] * 100
        fig.update_yaxes(range=[c * 0.97, max(d.charged_rate.max() * 100, c) * 1.04], row=1, col=i)
    fig.update_yaxes(title_text="effective MDR %", row=1, col=1)
    return _layout(fig, "Effective fee rate per day vs contract", 360, legend_below=True)


def fig_fee_cum(f: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    for meth, color in [("Wallet", C["claim"]), ("Card", C["break"])]:
        d = f[f.method == meth]
        fig.add_scatter(x=d.day_ist, y=d.cum_excess_inr, name=meth, mode="lines", line=dict(color=color, width=3),
                        hovertemplate="%{x|%d %b}: ₹%{y:,.2f}<extra>" + meth + "</extra>")
    fig.update_yaxes(title="₹ overcharged, incl. GST (cumulative)")
    return _layout(fig, "Overcharge building up through the month", 330)


def fig_settlements(s: pd.DataFrame) -> go.Figure:
    color = {"Credited on time": C["matched"], "Credited late": C["timing"], "Credited, amount differs": C["claim"],
             "In transit (not yet due)": C["timing"], "Overdue at close": C["break"]}
    fig = go.Figure()
    for st, d in s.groupby("status", sort=False):
        fig.add_bar(x=d.batch_date, y=d.net_inr / 1e5, name=f"{st} ({len(d)})", marker_color=color[st],
                    width=86_400_000 * 0.8,  # one day in ms: a single-bar trace on a date axis is otherwise 1 ms wide
                    customdata=d[["settlement_id", "due_date"]].astype(str),
                    hovertemplate="%{customdata[0]}<br>due %{customdata[1]}<br>₹%{y:.2f} L<extra>" + st + "</extra>")
    fig.update_yaxes(title="net payout, ₹ lakh")
    fig.update_xaxes(title="batch date (captures of that IST day)")
    return _layout(fig, "Every payout tracked to the bank", 340, barmode="overlay")


def fig_methods(mb: pd.DataFrame) -> go.Figure:
    mb = mb[mb.method != "Unknown"].sort_values("break_rate")
    fig = go.Figure(go.Bar(y=mb.method, x=mb.break_rate * 100, orientation="h", marker_color=C["break"],
                           text=[f"{r * 100:.2f}% · {b} of {k:,}" for r, b, k in zip(mb.break_rate, mb.break_keys, mb["keys"])],
                           textposition="outside", cliponaxis=False,
                           hovertemplate="%{y}: %{x:.2f}%<extra></extra>"))
    fig.update_xaxes(title="leg-1 break rate % (cut-off rows excluded)", range=[0, mb.break_rate.max() * 100 * 1.6])
    return _layout(fig, "Break rate by payment method", 300, showlegend=False, margin=dict(l=100, r=24, t=56, b=48))


# ---------------------------------------------------------------- page
CSS = """
:root{--ink:#1f2937;--muted:#6b7280;--line:#e5e7eb;--bg:#f3f4f6;--brand:#264653;--break:#d1495b;--ok:#2a9d8f;--amber:#e9b949;--claim:#6a4c93}
*{box-sizing:border-box} body{margin:0;background:var(--bg);font-family:"Segoe UI",Inter,Arial,sans-serif;color:var(--ink)}
.top{background:var(--brand);color:#fff;padding:16px 24px;display:flex;flex-wrap:wrap;gap:8px 24px;align-items:baseline}
.top h1{margin:0;font-size:20px;font-weight:600} .top span{opacity:.8;font-size:13px}
nav{display:flex;gap:2px;padding:0 16px;background:#fff;border-bottom:1px solid var(--line);overflow-x:auto}
nav button{border:0;background:none;padding:12px 16px;font:inherit;font-size:14px;color:var(--muted);cursor:pointer;border-bottom:3px solid transparent;white-space:nowrap}
nav button.on{color:var(--ink);border-bottom-color:var(--brand);font-weight:600}
.page{display:none;padding:16px;max-width:1400px;margin:0 auto} .page.on{display:block}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:12px}
.card{background:#fff;border:1px solid var(--line);border-left:4px solid var(--brand);border-radius:6px;padding:10px 14px}
.card.bad{border-left-color:var(--break)} .card.ok{border-left-color:var(--ok)} .card.amb{border-left-color:var(--amber)} .card.cl{border-left-color:var(--claim)}
.k{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em} .v{font-size:22px;font-weight:700;margin-top:2px} .s{font-size:12px;color:var(--muted)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px} .full{grid-column:1/-1}
.panel{background:#fff;border:1px solid var(--line);border-radius:6px;padding:4px;min-width:0}
.note{background:#fff;border:1px solid var(--line);border-radius:6px;padding:10px 16px;margin-bottom:12px;font-size:14px}
.note ul{margin:6px 0;padding-left:20px} .note li{margin:3px 0}
.slicers{display:flex;flex-wrap:wrap;gap:10px;background:#fff;border:1px solid var(--line);border-radius:6px;padding:10px 12px;margin-bottom:12px;align-items:end}
.slicers label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;display:flex;flex-direction:column;gap:4px}
.slicers select,.slicers input{font:inherit;font-size:13px;padding:6px 8px;border:1px solid var(--line);border-radius:4px;min-width:150px;background:#fff}
.slicers button{font:inherit;font-size:13px;padding:7px 12px;border:1px solid var(--brand);background:var(--brand);color:#fff;border-radius:4px;cursor:pointer}
.slicers button.ghost{background:#fff;color:var(--brand)}
.tbl{overflow:auto;max-height:520px} table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:6px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap} th{position:sticky;top:0;background:#f9fafb;font-weight:600}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;color:#fff} .pill.break{background:var(--break)} .pill.timing{background:var(--amber);color:#111} .pill.claim{background:var(--claim)}
footer{color:var(--muted);font-size:12px;padding:8px 16px 24px;max-width:1400px;margin:0 auto}
@media (max-width:900px){.grid{grid-template-columns:1fr}.cards{grid-template-columns:1fr 1fr}.top{padding:12px 16px}.page{padding:12px}}
"""

JS = """
const R = __REGISTER__;
const fmt = v => '₹' + v.toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2});
function show(id){
  document.querySelectorAll('.page').forEach(p => p.classList.toggle('on', p.id === id));
  document.querySelectorAll('nav button').forEach(b => b.classList.toggle('on', b.dataset.p === id));
  document.querySelectorAll('#' + id + ' .js-plotly-plot').forEach(g => Plotly.Plots.resize(g));
  try { history.replaceState(null, '', '#' + id); } catch (e) {}
}
document.querySelectorAll('nav button').forEach(b => b.onclick = () => show(b.dataset.p));
const sl = {owner: 'f-owner', cls: 'f-class', type: 'f-type', age: 'f-age'};
function opts(id, vals){ const s = document.getElementById(id);
  vals.forEach(v => { const o = document.createElement('option'); o.value = v; o.textContent = v; s.appendChild(o); }); }
opts(sl.owner, [...new Set(R.map(r => r.owner))].sort());
opts(sl.cls, ['break', 'timing', 'claim']);
opts(sl.type, [...new Set(R.map(r => r.type))].sort());
opts(sl.age, __AGES__);
function current(){
  const v = k => document.getElementById(sl[k]).value, q = document.getElementById('f-q').value.trim().toLowerCase();
  return R.filter(r => (!v('owner') || r.owner === v('owner')) && (!v('cls') || r.cls === v('cls'))
    && (!v('type') || r.type === v('type')) && (!v('age') || r.age === v('age'))
    && (!q || r.ref.toLowerCase().includes(q) || r.action.toLowerCase().includes(q)));
}
function render(){
  const rows = current().sort((a, b) => b.impact - a.impact);
  const tot = rows.reduce((s, r) => s + r.impact, 0), brk = rows.filter(r => r.cls === 'break');
  document.getElementById('x-items').textContent = rows.length.toLocaleString('en-IN');
  document.getElementById('x-value').textContent = fmt(tot);
  document.getElementById('x-breaks').textContent = brk.length.toLocaleString('en-IN');
  document.getElementById('x-old').textContent = rows.length ? Math.max(...rows.map(r => r.agen)) + ' days' : '–';
  const shown = rows.slice(0, 250);
  document.getElementById('x-tbody').innerHTML = shown.map(r => `<tr><td class="n">${r.id}</td><td><span class="pill ${r.cls}">${r.cls}</span></td>
    <td>${r.type}</td><td>${r.owner}</td><td>${r.ref}</td><td class="n">${fmt(r.impact)}</td><td>${r.date}</td><td class="n">${r.agen}</td><td>${r.action}</td></tr>`).join('');
  document.getElementById('x-shown').textContent = rows.length > shown.length ? `showing the largest ${shown.length} of ${rows.length}; export for all` : `${rows.length} items`;
}
Object.values(sl).concat(['f-q']).forEach(id => document.getElementById(id).addEventListener('input', render));
document.getElementById('f-reset').onclick = () => { Object.values(sl).forEach(id => document.getElementById(id).value = ''); document.getElementById('f-q').value = ''; render(); };
document.getElementById('f-csv').onclick = () => {
  const cols = ['id','cls','type','owner','ref','impact','date','agen','action'];
  const esc = v => '"' + String(v).replace(/"/g, '""') + '"';
  const csv = ['exception_id,class,type,owner,ref,impact_inr,event_date,age_days,action']
    .concat(current().map(r => cols.map(c => esc(r[c])).join(','))).join('\\n');
  const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([csv], {type: 'text/csv'}));
  a.download = 'exceptions_filtered.csv'; a.click();
};
render();
const h = (location.hash || '').slice(1); if (document.getElementById(h)) show(h);
"""


def card(k: str, v: str, s: str = "", cls: str = "") -> str:
    return f'<div class="card {cls}"><div class="k">{k}</div><div class="v">{v}</div><div class="s">{s}</div></div>'


def build_html(m: dict[str, pd.DataFrame], plotly_src: str) -> str:
    k = m["dash_kpis"].iloc[0]
    r, s, q = m["register"], m["dash_settlements"], m["dash_owner_queue"]
    sc = pd.read_csv(ROOT / "data" / "output" / "scorecard.csv")
    allsc = sc[(sc.issue == "ALL") & (sc.view == "in_scope")].iloc[0]
    held = s[s.status == "Overdue at close"].iloc[0]
    transit = s[s.status == "In transit (not yet due)"].iloc[0]
    fee = m["dash_fee_daily"]
    wal = fee[fee.method == "Wallet"].sort_values("day_ist")
    step_day = wal.loc[wal.charged_rate.diff().idxmax(), "day_ist"]
    top_share = r.query("`class` == 'break'").impact_inr.max() / k.break_inr
    gap = k.erp_net_sales_inr - k.bank_credits_inr

    figs = {n: f.to_html(full_html=False, include_plotlyjs=False, config={"displaylogo": False, "responsive": True})
            for n, f in {
                "bridge": fig_bridge(m["bridge"]), "daily": fig_daily(m["dash_daily_status"]),
                "types": fig_types(m["summary"]), "ageing": fig_ageing(q), "pareto": fig_pareto(r),
                "fees": fig_fees(fee), "feecum": fig_fee_cum(fee), "setl": fig_settlements(s),
                "methods": fig_methods(m["dash_method_breaks"])}.items()}

    age = lambda d: next(b for lim, b in [(-1, "not yet due"), (7, "0-7 d"), (14, "8-14 d"), (21, "15-21 d"),  # noqa: E731
                                          (10**6, "22+ d")] if d <= lim)
    reg_json = json.dumps([{
        "id": int(x["exception_id"]), "cls": x["class"], "type": TYPE_LABEL.get(x["exception_type"], x["exception_type"]),
        "owner": x["owner"], "ref": str(x["ref"]), "impact": round(float(x["impact_inr"]), 2),
        "date": str(x["event_date"])[:10], "agen": int(x["age_days"]), "age": age(int(x["age_days"])),
        "action": x["action"]} for x in r.to_dict("records")],
        ensure_ascii=False)

    claims = fee.groupby("method").agg(txns=("overcharged_txns", "sum"), excess=("excess_incl_gst_inr", "sum"))
    claims = claims[claims.txns > 0]
    claim_rows = "".join(f"<tr><td>{mt}</td><td class='n'>{int(c.txns):,}</td><td class='n'>₹{indian(c.excess)}</td></tr>"
                         for mt, c in claims.iterrows())
    setl_rows = "".join(
        f"<tr><td>{x.settlement_id}</td><td>{str(x.batch_date)[:10]}</td><td>{str(x.due_date)[:10]}</td>"
        f"<td>{'' if pd.isna(x.bank_date) else str(x.bank_date)[:10]}</td><td class='n'>₹{indian(x.net_inr)}</td><td>{x.status}</td></tr>"
        for x in s[s.status != "Credited on time"].itertuples())

    overview = f"""
<div class="cards">
 {card("ERP net sales", lakh(k.erp_net_sales_inr), "October, receipts + credit notes")}
 {card("Bank credits from gateway", lakh(k.bank_credits_inr), "PayFlux lines, October statement")}
 {card("Gap to explain", lakh(gap), "ERP − bank", "amb")}
 {card("Unexplained", "₹" + indian(k.unexplained_inr), "after the cash bridge", "ok")}
 {card("Auto-match rate", f"{k.auto_match_rate * 100:.2f}%", f"{int(k.leg1_keys):,} ledger ↔ gateway keys", "ok")}
 {card("Breaks to work", f"{int(k.break_items)}", lakh(k.break_inr) + " impact", "bad")}
 {card("80% of break ₹ in", f"{int(k.items_for_80pct)} items", f"of {int(k.break_items)} breaks", "bad")}
 {card("Payouts on time", f"{int(k.payouts_on_time)} / {int(k.payouts)}", f"{int((s.status == 'Overdue at close').sum())} overdue, {int((s.status == 'In transit (not yet due)').sum())} in transit", "amb")}
</div>
<div class="note"><b>What needs attention at this close</b><ul>
 <li><b>One payout is overdue:</b> the {dmy(held.batch_date)} batch ({lakh(held.net_inr)}) was due {dmy(held.due_date)} and is not in the bank. It is {top_share * 100:.1f}% of all break value. Treasury to escalate.</li>
 <li><b>Money out, not in the books:</b> {int((r.exception_type == 'REFUND_NO_CREDIT_NOTE').sum())} refunds with no credit note ({lakh(r.query("exception_type == 'REFUND_NO_CREDIT_NOTE'").impact_inr.sum())}) and {int((r.exception_type == 'CHARGEBACK_NOT_BOOKED').sum())} chargebacks ({lakh(r.query("exception_type == 'CHARGEBACK_NOT_BOOKED'").impact_inr.sum())}).</li>
 <li><b>Gateway charging above contract:</b> wallet MDR stepped up on {dmy(step_day)}; claim {lakh(k.claim_inr)} incl. GST and get the rate reset.</li>
 <li><b>Timing, no entry needed:</b> {lakh(k.timing_inr)} (31 Oct batch in transit {lakh(transit.net_inr)}, cut-off rows, credit notes pending).</li>
</ul></div>
<div class="grid">
 <div class="panel full">{figs["bridge"]}</div>
 <div class="panel">{figs["daily"]}</div>
 <div class="panel">{figs["types"]}</div>
</div>"""

    exceptions = f"""
<div class="slicers">
 <label>Owner<select id="f-owner"><option value="">All</option></select></label>
 <label>Class<select id="f-class"><option value="">All</option></select></label>
 <label>Type<select id="f-type"><option value="">All</option></select></label>
 <label>Age at close<select id="f-age"><option value="">All</option></select></label>
 <label>Search ref / action<input id="f-q" placeholder="ORD-…, setl_…, Wallet"></label>
 <button class="ghost" id="f-reset">Reset</button><button id="f-csv">Export CSV</button>
</div>
<div class="cards">
 <div class="card"><div class="k">Items in selection</div><div class="v" id="x-items"></div></div>
 <div class="card bad"><div class="k">₹ impact</div><div class="v" id="x-value"></div></div>
 <div class="card bad"><div class="k">of which breaks</div><div class="v" id="x-breaks"></div></div>
 <div class="card amb"><div class="k">Oldest item</div><div class="v" id="x-old"></div></div>
</div>
<div class="grid">
 <div class="panel">{figs["ageing"]}</div>
 <div class="panel">{figs["pareto"]}</div>
 <div class="panel full"><div style="padding:8px 10px;font-size:13px;color:var(--muted)">Work queue, largest ₹ first · <span id="x-shown"></span></div>
 <div class="tbl"><table><thead><tr><th class="n">#</th><th>Class</th><th>Type</th><th>Owner</th><th>Ref</th><th class="n">₹ impact</th><th>Date</th><th class="n">Age (d)</th><th>Action</th></tr></thead>
 <tbody id="x-tbody"></tbody></table></div></div>
</div>"""

    fees_page = f"""
<div class="cards">
 {card("Fees + GST charged", lakh(-m["bridge"].query("step == 7").amount_inr.iloc[0]), "October, all methods")}
 {card("Above contract", "₹" + indian(k.claim_inr), "incl. 18% GST", "cl")}
 {card("Overcharged txns", f"{int(fee.overcharged_txns.sum()):,}", "flagged per transaction", "cl")}
 {card("Wallet rate change", f"{dmy(step_day)}", f"{wal.charged_rate.iloc[0] * 100:.2f}% → {wal.charged_rate.iloc[-1] * 100:.2f}%", "bad")}
</div>
<div class="grid">
 <div class="panel full">{figs["fees"]}</div>
 <div class="panel">{figs["feecum"]}</div>
 <div class="panel"><div style="padding:12px 14px"><b>Claim to raise with the gateway</b>
  <table style="margin-top:8px"><thead><tr><th>Method</th><th class="n">Txns</th><th class="n">Excess incl. GST</th></tr></thead><tbody>{claim_rows}</tbody></table>
  <p class="s" style="margin-top:12px">Every capture is re-priced at the contracted MDR in <code>sql/30_fee_audit.sql</code>; the day the wallet rate moved is found with <code>LAG()</code>, not typed in. Small in rupees, but it compounds every month until the rate is reset.</p></div></div>
</div>"""

    setl_page = f"""
<div class="cards">
 {card("Payouts", f"{int(k.payouts)}", "30 Sep – 31 Oct batches")}
 {card("Credited on time", f"{int(k.payouts_on_time)}", "matched to bank by UTR, to the paisa", "ok")}
 {card("Overdue at close", lakh(held.net_inr), f"{held.settlement_id}, due {dmy(held.due_date)}", "bad")}
 {card("In transit", lakh(transit.net_inr), f"due {dmy(transit.due_date)}, timing", "amb")}
</div>
<div class="grid">
 <div class="panel full">{figs["setl"]}</div>
 <div class="panel">{figs["methods"]}</div>
 <div class="panel"><div style="padding:12px 14px"><b>Payouts not credited on time</b>
  <table style="margin-top:8px"><thead><tr><th>Settlement</th><th>Batch</th><th>Due</th><th>Bank</th><th class="n">Net</th><th>Status</th></tr></thead><tbody>{setl_rows}</tbody></table>
  <p class="s" style="margin-top:12px">Break rates by method are close (Day 2 chi-square p = 0.200), so breaks come from process, not one faulty integration.</p></div></div>
</div>"""

    js = JS.replace("__REGISTER__", reg_json).replace("__AGES__", json.dumps(AGE_ORDER))
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Month-End Reconciliation Dashboard</title>
<script src="{plotly_src}"></script>
<style>{CSS}</style></head><body>
<div class="top"><h1>Month-end reconciliation · October 2026</h1>
<span>ERP ledger ↔ PayFlux gateway ↔ bank · close date 31 Oct (IST) · synthetic data, portfolio project</span></div>
<nav><button data-p="p1" class="on">1 · Overview</button><button data-p="p2">2 · Exceptions</button>
<button data-p="p3">3 · Gateway fees</button><button data-p="p4">4 · Settlements</button></nav>
<section class="page on" id="p1">{overview}</section>
<section class="page" id="p2">{exceptions}</section>
<section class="page" id="p3">{fees_page}</section>
<section class="page" id="p4">{setl_page}</section>
<footer>Engine validated against injected ground truth: precision {allsc.precision * 100:.2f}%, recall {allsc.recall * 100:.2f}% in scope
({int(allsc.tp):,} of {int(allsc.labelled):,} problems caught, {int(allsc.fp)} false positives). Built by <code>src/dashboard.py</code> from <code>dashboard/marts.sql</code> ·
<a href="https://github.com/Rahman17309/transaction-reconciliation-engine">code on GitHub</a> · M A Rahman</footer>
<script>{js}</script>
</body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plotly-src", default=PLOTLY_CDN)
    ap.add_argument("--out", default=str(OUT_HTML))
    a = ap.parse_args()

    m, ties = load()
    for name, ok in ties:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not all(ok for _, ok in ties):
        return 1
    OUT_MARTS.mkdir(parents=True, exist_ok=True)
    for n in ["dash_kpis", "dash_daily_status", "dash_owner_queue", "dash_fee_daily", "dash_settlements", "dash_method_breaks"]:
        m[n].to_csv(OUT_MARTS / f"{n.removeprefix('dash_')}.csv", index=False, float_format="%.6f")
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(m, a.plotly_src), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size / 1024:.0f} KB) and {OUT_MARTS.relative_to(ROOT)}/*.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
