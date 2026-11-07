"""Build the phase-1 audit report (static HTML) from a run folder."""
import csv, collections, html, json, sys

run, out = sys.argv[1], sys.argv[2]
c = list(csv.DictReader(open(f"{run}/roce_current.csv")))
u = list(csv.DictReader(open(f"{run}/universe.csv")))
s = json.load(open(f"{run}/summary.json"))
e = [r for r in c if r["rank_eligible"] == "True"]
E = html.escape
pct = lambda x: "N/A" if x in ("", None) else f"{float(x)*100:.1f}%"
bn = lambda x, cur="USD": "N/A" if x in ("", None) else f"{float(x)/1e6:,.0f}" + ("" if cur == "USD" else f" {cur}")
FLAG_CN = {
 "CE_NEGATIVE_OR_ZERO": "资本为零或负数（N/M）", "CE_NEAR_ZERO": "资本基数接近零（N/M）", "NWC_NEGATIVE": "净营运资本为负",
 "GOODWILL_HEAVY": "商誉 > 资本 40%", "EQUITY_NEGATIVE": "股东权益为负（常因回购）", "BIG_IMPAIRMENT": "大额减值",
 "BIG_ACQUISITION": "大额并购", "ONE_OFF_GAIN": "大额处置收益", "LEASE_HEAVY": "租赁负债 > 资本 20%", "CE_JUMP": "资本投入大增 >1.5 倍",
 "STALE": "数据过期（不排名）", "ROCE_DROP_QOQ": "比上季下降 ≥5 个百分点", "LARGE_ADJUSTMENT": "调整项 > EBIT 20%",
 "EBIT_FALLBACK": "备用 EBIT 口径", "EBIT_CONFLICT": "营业利润与税前利润方向相反", "EBIT_GT_REVENUE": "营业利润 > 收入（疑似标签错误，不排名）",
 "NO_QUARTERLY_XBRL": "只有年报（20-F/40-F 等）", "EBIT_METHOD_MIXED_IN_HISTORY": "历史年份混用 EBIT 口径",
 "INTEREST_NOT_TAGGED": "未标注利息费用", "TTM_FROM_QUARTERS": "TTM 由四个季度相加"}
fl = lambda r: " ".join(f'<span class="f" title="{E(FLAG_CN.get(x, x))}">{E(FLAG_CN.get(x, x))}</span>' for x in r["flags"].split())

def table(rows, cols, tid=""):
    h = "".join(f"<th>{E(n)}</th>" for n, _ in cols)
    b = "".join("<tr>" + "".join(f"<td>{f(r)}</td>" for _, f in cols) + "</tr>" for r in rows)
    return f'<table id="{tid}"><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table>'

MAIN = [("#", None), ("代码", lambda r: f'<b>{E(r["ticker"])}</b>'), ("公司", lambda r: E(r["name"][:28])),
        ("SIC 行业", lambda r: E(r["sic_description"][:26])), ("期末", lambda r: r["period_end"]), ("SEC 提交", lambda r: r["latest_filed"]),
        ("TTM EBIT（百万）", lambda r: bn(r["ebit_ttm"], r["currency"])), ("平均资本（百万）", lambda r: bn(r["ce_avg"], r["currency"])),
        ("ROCE", lambda r: f'<b>{pct(r["roce_ttm"])}</b>'), ("调整后", lambda r: pct(r["roce_ttm_adj"])),
        ("5年均值", lambda r: pct(r["roce_5y_avg"])), ("5年≥20%", lambda r: f'{r["years_ge_20"]}/{r["years_available"]}'),
        ("趋势", lambda r: r["trend"]), ("稳定性", lambda r: r["stability_grade"] or "—"), ("数据质量", lambda r: r["data_quality"]),
        ("标记", fl)]
def numbered(rows):
    for i, r in enumerate(rows, 1):
        r = dict(r); r["_i"] = i; yield r
MAIN[0] = ("#", lambda r: r["_i"])

excl = collections.Counter(r["exclude_reason"] for r in u if r["exclude_reason"])
stat = collections.Counter(r["status"] for r in c)
flags = collections.Counter(f for r in c for f in r["flags"].split())
q3 = sum(1 for r in e if r["period_end"] >= "2026-08-01")
A = [r for r in c if r["stability_grade"] == "A"]
A.sort(key=lambda r: -float(r["roce_ttm"] or 0))
watch = [r for r in c if r["watchlist"] == "True"]
fin = [r for r in c if r["group"] in ("financial", "reit")]
tiles = [("覆盖公司", f'{s["covered"]:,}', "Nasdaq/NYSE 普通股，排除 ETF/基金/SPAC"), ("可排名普通企业", f'{len(e):,}', "ROCE 有效、非金融/REIT、数据未过期"),
         ("ROCE ≥ 40%", s["roce_ge_40"], "可排名普通企业"), ("ROCE 30–40%", s["roce_30_40"], "可排名普通企业"),
         ("连续 5 年 ≥ 20%", s["five_years_ge_20"], "稳定性 A 级"), ("观察池", s["watchlist"], "≥30%、5年中≥3年≥20%、无严重标记")]
T = "".join(f'<div class="t"><div class="n">{v}</div><div class="l">{E(k)}</div><div class="s">{E(d)}</div></div>' for k, v, d in tiles)

doc = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>美股好公司雷达｜第一阶段数据与 ROCE 引擎（2026-11-07）</title><style>
body{{font:14px/1.55 -apple-system,"PingFang SC","Noto Sans SC",sans-serif;margin:0;color:#1d1d1f;background:#fafafa}}
main{{max-width:1280px;margin:auto;padding:24px}} h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:17px;margin:32px 0 8px;border-bottom:1px solid #ddd;padding-bottom:4px}}
.sub{{color:#666}} .warn{{background:#fff7e0;border-left:4px solid #e0a800;padding:10px 14px;margin:14px 0}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:16px 0}}
.t{{background:#fff;border:1px solid #e3e3e3;border-radius:8px;padding:12px}} .n{{font-size:24px;font-weight:600}} .l{{font-weight:600}} .s{{color:#777;font-size:12px}}
table{{border-collapse:collapse;width:100%;background:#fff;font-size:12.5px}} th,td{{border-bottom:1px solid #eee;padding:5px 6px;text-align:left;vertical-align:top}}
th{{background:#f2f2f2;position:sticky;top:0}} .f{{display:inline-block;background:#f1f1f1;border-radius:4px;padding:0 5px;margin:1px;font-size:11px;color:#555}}
.wrap{{overflow-x:auto}} input{{padding:6px 10px;width:280px;border:1px solid #ccc;border-radius:6px}} code{{background:#f0f0f0;padding:0 4px;border-radius:3px}}
.two{{display:grid;grid-template-columns:1fr 1fr;gap:16px}} @media(max-width:800px){{.two{{grid-template-columns:1fr}}}}
</style></head><body><main>
<h1>美股好公司雷达｜第一阶段：股票库、SEC 数据采集、ROCE 计算引擎</h1>
<div class="sub">数据截至 SEC 文件下载时间 2026-11-07 06:23–06:38 UTC（已核实，来源 sec.gov companyfacts.zip / submissions.zip）。计算运行 {E(s["run_utc"])}。</div>
<div class="warn"><b>只研究企业质量，不是买卖建议。</b>ROCE 高不代表股价便宜。本页是第一阶段的引擎验收结果：行业分类（GICS）、周期高峰检查、竞争优势解释属于第二到第四阶段，尚未完成。</div>
<div class="tiles">{T}</div>

<h2>1. 口径（所有公司相同）</h2>
<p><b>ROCE = TTM EBIT ÷ 平均已动用资本</b>；已动用资本 = 总资产 − 流动负债；平均 = （一年前季末 + 本季末）÷ 2。EBIT 主口径为 10-Q/10-K 中的营业利润（OperatingIncomeLoss）；未标注营业利润的公司改用“税前持续经营利润 + 利息费用”，并标记“备用 EBIT 口径”（本次 {sum(r["ebit_method"]=="B" for r in e)} 家可排名公司）。TTM = 本年累计 + 上一财年 − 上年同期累计。资本为零或负数、或资本低于总资产 10% 的公司显示 N/M，不排名。缺失一律 N/A，没有任何估算值。完整说明见 GitHub 上的 METHODOLOGY.md。</p>
<p><b>抽查（已核实）：</b>苹果 FY2025（截至 2025-09-27）营业利润 $133.05B；总资产 $359.241B − 流动负债 $165.631B = 资本 $193.61B；上年末资本 $188.588B；ROCE = 133.05 ÷ 191.099 = <b>69.6%</b>，与引擎输出一致（来源 accession 0000320193-26-000026）。</p>

<h2>2. 覆盖与排除</h2><div class="two"><div>
{table([{"k":k,"v":v} for k,v in [("SEC 代码表中的公司（CIK）", s["universe_total"])] + excl.most_common() + [("进入计算", s["covered"])]], [("项目", lambda r: E(r["k"])), ("公司数", lambda r: f'{r["v"]:,}')])}
<p class="sub">SEC 代码表把 NYSE American 归入 “NYSE”，所以没有单独计数。金融 {s["groups"]["financial"]} 家、REIT {s["groups"]["reit"]} 家已计算但不进普通企业排名。</p></div><div>
{table([{"k":k,"v":v} for k,v in stat.most_common()], [("计算状态", lambda r: E(r["k"])), ("公司数", lambda r: f'{r["v"]:,}')])}
<p class="sub">“资产负债表未区分流动负债”主要是银行、保险、REIT、公用事业和房屋建筑商，第二阶段为它们单独设计指标。本季已更新到 2026 年 8 月之后期末的可排名公司：{q3:,} 家；其余显示上一期有效数据。</p></div></div>

<h2>3. 初步排名（可排名普通企业，按 TTM ROCE）</h2>
<p><input id="q" placeholder="搜索代码或公司名" oninput="f()"> <span class="sub">共 {len(e):,} 家，显示前 150（完整名单在 roce_current.csv）。金额单位为百万，非美元公司标出币种。</span></p>
<div class="wrap">{table(list(numbered(e[:150])), MAIN, "rk")}</div>

<h2>4. 连续 5 年 ROCE ≥ 20%（稳定性 A 级，{len(A)} 家）</h2>
<div class="wrap">{table(list(numbered(A)), MAIN)}</div>

<h2>5. 数据失真标记统计</h2>
{table([{"k":k,"v":v} for k,v in flags.most_common()], [("标记", lambda r: E(FLAG_CN.get(r["k"], r["k"]))), ("代码", lambda r: f'<code>{r["k"]}</code>'), ("公司数", lambda r: r["v"])])}

<h2>6. 已知局限（第二阶段处理）</h2><ul>
<li>行业目前只有 SEC 的 SIC 描述，还不是 GICS 一级/二级分类；半导体尚未独立分组。</li>
<li>周期高峰检查需要行业数据，尚未做（存储芯片 SNDK、MU、STX、WDC 当前 ROCE 很高，正是需要这项检查的例子）。</li>
<li>调整后 ROCE 依赖公司在 XBRL 中标注的减值、重组和处置收益科目；未标注的一次性项目无法识别。</li>
<li>没有市值和股价，暂不设规模门槛，名单里有资本只有几百万美元的微型公司。</li>
<li>第二口径（权益 + 有息负债 − 非经营现金）尚未加入交叉核对。</li>
<li>完成合并后仍保留 SIC 6770 的公司会被当作 SPAC 排除；BDC 按封闭式基金排除。</li></ul>
<h2>7. 文件</h2><p>项目文件夹 <code>us-quality-radar/out/2026Q3_run_2026-11-07/</code>：universe.csv（全部公司及排除原因）、roce_current.csv（每家公司一行）、roce_annual.csv（每家公司每财年一行，最多 10 年）、audit.zip（每家公司的全部输入数字、SEC accession 与原文链接）。代码在 GitHub zakiyang9333/commerce-data-check 分支 claude/project-thread-6zysa2 的 us_quality_radar/。</p>
</main><script>
function f(){{var q=document.getElementById('q').value.toLowerCase();document.querySelectorAll('#rk tbody tr').forEach(function(t){{t.style.display=t.textContent.toLowerCase().indexOf(q)>-1?'':'none'}})}}
</script></body></html>"""
open(out, "w").write(doc)
print(len(doc))
