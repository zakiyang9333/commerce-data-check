"""Build the phase-2 report HTML from a phase-2 output folder."""
import csv, html, json, sys
from collections import defaultdict

run, out = sys.argv[1], sys.argv[2]
R = list(csv.DictReader(open(f"{run}/phase2_companies.csv")))
S = json.load(open(f"{run}/summary2.json"))
B = json.load(open(f"{run}/industry_boards.json"))
E = html.escape
EL = [r for r in R if r["rank_eligible"] == "True"]
fv = lambda v: float(v) if v not in ("", None) else None
def pct(v):
    x = fv(v)
    return ("N/A", "-9e9") if x is None else (f"{x*100:.1f}%", f"{x}")
CN = {"Information Technology": "信息技术", "Semiconductors": "半导体", "Communication Services": "通信服务",
      "Consumer Discretionary": "可选消费", "Consumer Staples": "日常消费", "Health Care": "医疗保健", "Industrials": "工业",
      "Energy": "能源", "Materials": "原材料", "Utilities": "公用事业", "Financials": "金融", "Real Estate": "房地产", "Unclassified": "未分类"}

def cell(text, sortv=None):
    return f'<td data-v="{E(str(sortv if sortv is not None else text))}">{text}</td>'
COLS = {
 "tk": ("代码", lambda r: cell(f'<b>{E(r["ticker"])}</b>', r["ticker"])),
 "nm": ("公司", lambda r: cell(E(r["name"][:26]), r["name"])),
 "cn": ("中文名", lambda r: cell(E(r["name_cn"] or "—"), r["name_cn"])),
 "sec": ("板块", lambda r: cell(E(CN.get(r["radar_sector"], r["radar_sector"])))),
 "ind": ("GICS 行业 / 子行业", lambda r: cell(E(r["gics_industry"]) + ("<br><span class=s>" + E(r["gics_sub_industry"]) + "</span>" if r["gics_sub_industry"] != "Unclassified" else ""))),
 "seg": ("细分", lambda r: cell(E(r["radar_segment"] or "—"))),
 "conf": ("分类可信度", lambda r: cell(E(r["class_confidence"]))),
 "cyc": ("周期", lambda r: cell(E(r["cyclical_class"] + " " + r["cyclical_label"]), r["cyclical_class"])),
 "cur": ("当前 ROCE", lambda r: cell("<b>%s</b>" % pct(r["roce_ttm"])[0], pct(r["roce_ttm"])[1])),
 "a3": ("3年均值<br><span class=s>满3年</span>", lambda r: cell(*pct(r["roce_3y_avg_full"]))),
 "a5": ("5年均值<br><span class=s>同第一阶段，有效年数</span>", lambda r: cell(pct(r["roce_5y_avg"])[0] + (f'<span class=s>（{int(fv(r["years_available"]))}年）</span>' if fv(r["years_available"]) is not None else ""), pct(r["roce_5y_avg"])[1])),
 "a5f": ("5年均值<br><span class=s>满5年，分类用</span>", lambda r: cell(*pct(r["roce_5y_avg_full"]))),
 "m5": ("5年中位数<br><span class=s>满5年</span>", lambda r: cell(*pct(r["roce_5y_median_full"]))),
 "m10": ("10年中位数", lambda r: cell(*pct(r["roce_10y_median"]))),
 "nr": ("Normalized", lambda r: cell(*pct(r["normalized_roce"])) if r["normalized_status"] == "OK" else cell('<span class=s>数据不足</span>', -9e9)),
 "vol": ("波动σ", lambda r: cell(f'{fv(r["roce_volatility_pp"]):.0f}pp' if fv(r["roce_volatility_pp"]) is not None else "N/A", r["roce_volatility_pp"] or -1)),
 "dd": ("最大回落", lambda r: cell(f'{fv(r["roce_max_drawdown_pp"]):.0f}pp' if fv(r["roce_max_drawdown_pp"]) is not None else "N/A", r["roce_max_drawdown_pp"] or -1)),
 "ocf": ("现金流验证", lambda r: cell(E(r["ocf_verified"]))),
 "q": ("质量类", lambda r: cell(f'<b>{E(r["quality_class"])}</b>')),
 "why": ("理由", lambda r: cell(f'<span class=s>{E(r["quality_reason"])}</span>')),
 "sig": ("高峰信号", lambda r: cell("<br>".join(f"<span class=s>{E(x)}</span>" for x in r["peak_signals"].split(" | ") if x))),
 "slope": ("趋势斜率<br><span class=s>同第一阶段</span>", lambda r: cell(f'{fv(r["trend_slope_pp"]):+.1f}pp/年' if fv(r["trend_slope_pp"]) is not None else "N/A", r["trend_slope_pp"] or -99)),
 "qs": ("D/E 判定斜率<br><span class=s>近5财年</span>", lambda r: cell(f'{fv(r["qclass_slope_pp"]):+.1f}pp/年' if fv(r["qclass_slope_pp"]) is not None else "N/A", r["qclass_slope_pp"] or -99)),
 "pe": ("期末", lambda r: cell(r["period_end"])),
}
def table(rows, keys, tid, note=""):
    th = "".join(f'<th onclick="srt(this)">{COLS[k][0]}</th>' for k in keys)
    tb = "".join("<tr>" + "".join(COLS[k][1](r) for k in keys) + "</tr>" for r in rows)
    return f'<p class=s>{note}共 {len(rows)} 家。点表头排序。</p><div class=wrap><table id="{tid}"><thead><tr>{th}</tr></thead><tbody>{tb}</tbody></table></div>'
by_roce = lambda rs: sorted(rs, key=lambda r: -(fv(r["roce_ttm"]) or -9))

conf = S["eligible_confidence"]
n = S["rank_eligible"]
cls_tbl = "".join(f"<tr><td>{k}</td><td>{v:,}</td><td>{v/n:.1%}</td></tr>" for k, v in conf.items())
sec_rows = []
groups = defaultdict(list)
for r in EL:
    groups[r["radar_sector"]].append(r)
for b in B["sector"]:
    k = b["key"]
    top = by_roce(groups[k])[:20]
    det = table(top, ["tk", "nm", "cn", "ind", "seg", "cur", "a5", "m5", "nr", "cyc", "q"], f"sec_{abs(hash(k))}")
    sec_rows.append(f'<tr><td><details><summary><b>{E(CN.get(k, k))}</b> {E(k)}</summary>{det}</details></td><td>{b["n"]}</td><td>{b["mean"]*100:.1f}%</td><td>{b["median"]*100:.1f}%</td><td>{b["n_ge_30"]}</td><td>{b["n_ge_40"]}</td><td>{b["n_5y_ge_20"]}</td></tr>')
seg_rows = "".join(f'<tr><td>{E(b["key"])}</td><td>{b["n"]}</td><td>{b["mean"]*100:.1f}%</td><td>{b["median"]*100:.1f}%</td><td>{b["n_ge_30"]}</td><td>{b["n_ge_40"]}</td><td>{b["n_5y_ge_20"]}</td></tr>'
                   for b in B["segment"] if b["key"] and b["n"] >= 1)
ind_rows = "".join(f'<tr><td>{E(b["key"])}</td><td>{b["n"]}</td><td>{b["mean"]*100:.1f}%</td><td>{b["median"]*100:.1f}%</td><td>{b["n_ge_30"]}</td><td>{b["n_ge_40"]}</td><td>{b["n_5y_ge_20"]}</td></tr>'
                   for b in B["industry"] if b["n"] >= 5)
BH = "<tr><th>名称</th><th>公司数</th><th>ROCE 平均</th><th>ROCE 中位数</th><th>≥30%</th><th>≥40%</th><th>连续5年≥20%</th></tr>"

AB = sorted([r for r in EL if r["quality_class"] in ("A", "B")], key=lambda r: -(fv(r["roce_5y_median_full"]) or -9))
CC = by_roce([r for r in EL if r["quality_class"] == "C" and r["cyclical_class"] in ("A", "B")])
CN_ = by_roce([r for r in EL if r["quality_class"] == "C" and r["cyclical_class"] not in ("A", "B")])
PK = by_roce([r for r in EL if r["peak_alert"]])
D = sorted([r for r in EL if r["quality_class"] == "D"], key=lambda r: -(fv(r["qclass_slope_pp"]) or -99))
EE = sorted([r for r in EL if r["quality_class"] == "E"], key=lambda r: (fv(r["qclass_slope_pp"]) or 0))
UN = [r for r in EL if r["gics_sector"] == "Unclassified" or r["gics_sub_industry"] == "Unclassified"]
DI = [r for r in EL if r["quality_class"] == "数据不足"]
NI = sum(1 for r in EL if r["normalized_status"] != "OK")
mem = [r for r in EL if r["radar_segment"] in ("存储芯片", "存储设备（硬盘）")]
q = S["quality_class"]
tiles = [("可排名普通企业", f"{n:,}", "第一阶段结果，未改动"), ("板块分类完成率", f'{S["eligible_classified_sector"]/n:.1%}', f'{S["eligible_classified_sector"]:,} 家'),
         ("子行业分类完成率", f'{S["eligible_classified_sub_industry"]/n:.1%}', f'{S["eligible_classified_sub_industry"]:,} 家；其余子行业为 Unclassified'),
         ("A 长期优秀", q["A"], "规则筛选"), ("B 优秀", q["B"], ""), ("C 周期性/未持续", q["C"], ""),
         ("D 成长改善", q["D"], ""), ("E 质量恶化", q["E"], ""), ("周期高峰警报", len(PK), "可排名公司")]
T = "".join(f'<div class=t><div class=n>{v}</div><div class=l>{E(k)}</div><div class=s>{E(d)}</div></div>' for k, v, d in tiles)
p1 = S["phase1_sha256"]

doc = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>美股好公司雷达第二阶段报告（2026-11-07）</title><style>
body{{font:14px/1.55 -apple-system,"PingFang SC","Noto Sans SC",sans-serif;margin:0;color:#1d1d1f;background:#fafafa}}
main{{max-width:1320px;margin:auto;padding:24px}} h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:17px;margin:34px 0 8px;border-bottom:1px solid #ddd;padding-bottom:4px}}
.s{{color:#666;font-size:12px}} .warn{{background:#fff7e0;border-left:4px solid #e0a800;padding:10px 14px;margin:14px 0}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:16px 0}}
.t{{background:#fff;border:1px solid #e3e3e3;border-radius:8px;padding:12px}} .n{{font-size:22px;font-weight:600}} .l{{font-weight:600}}
table{{border-collapse:collapse;width:100%;background:#fff;font-size:12.5px}} th,td{{border-bottom:1px solid #eee;padding:5px 6px;text-align:left;vertical-align:top}}
th{{background:#f2f2f2;cursor:pointer;position:sticky;top:0}} .wrap{{overflow-x:auto;max-height:720px;overflow-y:auto}} details summary{{cursor:pointer}}
nav a{{margin-right:12px}} code{{background:#f0f0f0;padding:0 4px;border-radius:3px}}
</style></head><body><main>
<h1>美股好公司雷达第二阶段报告：行业分类与周期高峰识别</h1>
<div class=s>数据：SEC companyfacts（2026-11-07 06:30 UTC 下载，已核实）；第二阶段运行 {E(S["run_utc"])}。第一阶段文件未改动，SHA-256：roce_current {p1["roce_current.csv"][:12]}…，roce_annual {p1["roce_annual.csv"][:12]}…，universe {p1["universe.csv"][:12]}…</div>
<div class=warn><b>研究筛选规则，不是投资评级，也不是买卖建议。</b>行业分类除 S&P 500 外均为 SEC SIC 推出的替代分类，不是官方授权 GICS。B 类的“竞争优势”尚未逐家验证。存储芯片未接入 DRAM/NAND 价格数据。</div>
<nav><a href="#c1">1 分类完成率</a><a href="#c2">2 行业排行</a><a href="#c3">3 长期优秀</a><a href="#c4">4 周期性高 ROCE</a><a href="#c5">5 周期高峰警报</a><a href="#c6">6 持续改善</a><a href="#c7">7 质量恶化</a><a href="#c8">8 数据不足与无法分类</a></nav>
<div class=tiles>{T}</div>

<div class=s style="margin:8px 0;padding:8px;border-left:3px solid #c80"><b>口径说明（11-09 修订）</b>：「5年均值（同第一阶段）」与第一阶段报告完全相同，是最近 5 个财年里有效年份的平均，有效年可能只有 1–4 年，括号里是年数。「5年均值（满5年）」要求最近 5 个财年全部有效，否则 N/A，只用于质量分类和高峰信号 S1。旧版第二阶段报告把后者也叫「5年均值」，且会跳过 N/M 年份往更早取数，所以与第一阶段对不上（1,124 家），本版已改正。</div>
<h2 id=c1>1. GICS 行业分类完成率（可排名普通企业）</h2>
<table><tr><th>分类可信度</th><th>公司数</th><th>占比</th></tr>{cls_tbl}</table>
<p class=s>高 = S&P 500 公开 GICS；中 = SIC 对照到子行业；中低 = 只到行业；低 = 只到板块；无 = Unclassified。不根据公司名称猜测。细分赛道（GPU、SaaS 等）为人工整理清单，标“待核实”。</p>

<h2 id=c2>2. 行业质量排行榜（按行业 ROCE 中位数）</h2>
<p class=s>平均值容易被少数极端公司拉高，所以同时给出中位数，并按中位数排序。点击板块名称展开该板块 ROCE 最高的 20 家公司。</p>
<table>{BH}{"".join(sec_rows)}</table>
<h3>半导体与软件细分赛道</h3><table>{BH}{seg_rows}</table>
<details><summary><b>GICS 行业层排行（≥5 家公司）</b></summary><table>{BH}{ind_rows}</table></details>

<h2 id=c3>3. 长期优秀企业排行榜（A 类 {q["A"]} 家，B 类 {q["B"]} 家）</h2>
<p class=s>并列展示 5 年均值、5 年中位数和当前 ROCE，不把三者相加成综合分。默认按 5 年中位数排序（最不受单年异常影响），可点表头改按其他指标排序。</p>
{table(AB, ["tk","nm","cn","sec","ind","seg","q","m5","a5f","a5","cur","m10","nr","dd","ocf","cyc"], "ab")}

<h2 id=c4>4. 周期性高 ROCE 企业（C 类中属于强/中等周期行业）</h2>
<p class=s>当前 ROCE ≥30%，但处于周期行业且有高峰依赖或暴涨暴跌历史。Normalized ROCE 是近 7–10 年 EBIT 合计 ÷ 资本合计。</p>
{table(CC, ["tk","nm","cn","seg","ind","cur","a5","a5f","m10","nr","dd","sig","why"], "cc")}
<details><summary><b>C 类中的非周期行业公司（高回报未持续，{len(CN_)} 家）</b></summary>{table(CN_, ["tk","nm","cn","ind","cur","a5","m5","nr","dd","why"], "cn")}</details>
<p class=s>存储芯片和硬盘公司 {len(mem)} 家：DRAM/NAND 价格数据未接入，判断只依据财报毛利率、库存与资本开支。</p>

<h2 id=c5>5. 周期高峰风险名单（{len(PK)} 家）</h2>
<p class=s>规则：周期行业出现 ≥2 个信号且含“高于 5 年均值”或“高于 10 年中位数”，或任何公司出现 ≥3 个信号。每个信号都列出具体数字。弱周期公司的警报只作提示，不降级。</p>
{table(PK, ["tk","nm","cn","seg","cyc","cur","a5","m10","nr","sig","q"], "pk")}

<h2 id=c6>6. ROCE 持续改善企业（D 类 {q["D"]} 家）</h2>
<p class=s>当前 10%–30%，最近 4–5 年 ROCE 斜率 ≥ +2 个百分点/年，最多一次下降。默认按斜率排序。</p>
{table(D, ["tk","nm","cn","ind","cur","a3","a5","a5f","qs","why","cyc"], "dd")}

<h2 id=c7>7. 质量恶化企业（E 类 {q["E"]} 家）</h2>
{table(EE, ["tk","nm","cn","ind","cur","a5","a5f","m10","qs","why","cyc"], "ee")}

<h2 id=c8>8. 数据不足及无法分类名单</h2>
<p>Normalized ROCE 数据不足（有效财年少于 7 年）：{NI:,} 家可排名公司，表中显示“数据不足”，没有用当前 ROCE 代替。</p>
{table(DI, ["tk","nm","cn","ind","cur","why"], "di", "高 ROCE 但完整财年少于 3 年：")}
<details><summary><b>子行业或板块无法分类（{len(UN):,} 家）</b></summary>{table(by_roce(UN), ["tk","nm","cn","sec","ind","conf","cur"], "un")}</details>
<h2>文件</h2><p class=s>项目文件夹 us-quality-radar/out/phase2_2026-11-07/：phase2_companies.csv（每家公司一行，全部新指标）、industry_boards.json、summary2.json、audit2.zip（每家公司新增输入的 SEC accession 与链接）。方法见 GitHub 分支 claude/project-thread-6zysa2 的 us_quality_radar/phase2/METHODOLOGY_PHASE2.md。</p>
</main><script>
function srt(th){{var t=th.closest('table'),i=[].indexOf.call(th.parentNode.children,th),b=t.tBodies[0],rs=[].slice.call(b.rows),d=th.dataset.d==='1'?-1:1;th.dataset.d=d===1?'1':'0';
rs.sort(function(a,c){{var x=a.cells[i].dataset.v,y=c.cells[i].dataset.v,nx=parseFloat(x),ny=parseFloat(y);if(!isNaN(nx)&&!isNaN(ny))return d*(ny-nx);return d*String(x).localeCompare(String(y))}});rs.forEach(function(r){{b.appendChild(r)}})}}
</script></body></html>"""
open(out, "w").write(doc)
print(len(doc), len(AB), len(CC), len(PK), len(D), len(EE), len(UN))
