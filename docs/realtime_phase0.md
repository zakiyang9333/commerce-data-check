# Realtime layer / 近实时层 — PLAN_B+ Phase 0

`realtime/` 是独立于 `check_data.py` 的新包，Python 3.9+，仅用标准库。它提供 Phase 0 的基础设施与 shadow 能力，不打开实时 UI，不发生产告警，不切换生产模型，不改 T+1 settled fact。

## Frozen contract / 冻结原则

```
realtime collection → completeness/freshness → realtime fact → deterministic signal
→ inference queue → inference cache → dashboard materialization
```

`realtime/contract.py` 固定：`FACT_REFRESH_NOT_BLOCKED_BY_MIMO`、`MIMO_FAILURE_DOES_NOT_FAIL_DATA_REFRESH`、`MIMO_OUTPUT = MODEL_INFERENCE`、`AUTO_EXECUTION = 0`、`PRODUCTION_ALERTS_ENABLED = False`、`REALTIME_UI_ENABLED = False`。模型输出中的 `gmv / roi / fee_rate / freshness / anomaly_threshold / stop_decision / budget_execution` 以及任何执行类字段会被剔除并计入 `mimo_ownership_violation_n`。

## Components / 组件

| Phase 0 | 模块 | 要点 |
|---|---|---|
| 0-A MiMo 解耦 | `refresh.py`, `inference.py` | `run_chain` 先跑完全部 fact/materialize 步骤，再把 inference 步骤转成队列行；入队只是本地写库，不等模型。队列失败 → `inference_status = FAILED`，`fact_refresh` 仍为 PASS。 |
| 0-B realtime store | `store.py`, `db.py` | 独立 `realtime_fact.db`（SQLite + WAL）。拒绝打开 `settled_store_paths` 以及任何非本层创建的数据库。每行 `provisional = 1`（CHECK 约束）。 |
| 0-C 专用浏览器 | `browser.py` | 自选无冲突端口（跳过 `reserved_ports`，默认含 9330、9222），独立 profile（带标记文件，拒绝人工浏览 profile），独立文件锁，按用途白名单，CDP 探活 + 登录健康，崩溃自动重启（带退避），只停止自己登记且核验过的进程。 |
| 0-D 5 分钟采集 | `schedule.py`, `collector.py` | 08:00–24:00 每 5 分钟、00:00–08:00 每 30 分钟，共 208 个 slot/天（UTC+8）。只接受 catalog 中的日内指标；SKU settled、search、AI客服、price、consumer intent 在配置加载和写入时双重拒绝。 |
| 0-E 信号 shadow | `signals.py` | `REALTIME_SIGNAL_ENGINE_V1`：GMV_DROP、ORDER_DROP、SPEND_SPIKE、ROI_COLLAPSE、CAMPAIGN_SPEND_ANOMALY。仅确定性规则，`mode = SHADOW`，`production_alert = 0`（CHECK 约束）。 |
| 0-F MiMo 2.6 shadow | `inference.py` | lane `production`（mimo-v2.5-pro，唯一可服务看板）与 `MIMO26_REALTIME_SHADOW_V1`（CHALLENGER，禁止 `serve_to_ui`）。model 未填时 lane 关闭，记 `NO_MIMO_CALL / LANE_DISABLED`。 |
| payload | `payload.py`, `pipeline.py` | 页面只读 latest good 与已缓存的推理视图，从不入队。 |
| shadow 日报 | `shadow.py` | 第十五节全部字段。 |

## Latest-good semantics / 最新有效值

- `latest_attempt`：最近一次尝试（任何状态）。
- `latest_success`：最近一次 `SUCCESS`。
- `latest_good`：每个序列 `quality = GOOD` 且 event_time 最大的值。
- 失败 / 跳过的尝试不写 observation，不可能覆盖或清空有效值；每次写入前后比对 required 序列，计入 `erased_good_n`（应恒为 0）。
- 累计指标出现回落时先记 `SUSPECT`，连续确认后才采用；跌到 0 永不自动采用（通常是页面没加载完）。
- 同一 event_time 的重复值去重；不同值记为 revision（`RESTATED`）；早于已有值的迟到数据保留但不替换 latest good。
- Completeness 只统计本次尝试中产出 GOOD 值的 required 序列。

Payload 默认消费 latest good，同时给出 `data_as_of`、`last_success_at`、`latest_attempt_status`、`lag_minutes`、`completeness`、`provisional = true`、`source_status`，展示文案为「今日近实时 · 更新至 14:35 · 延迟约 4 分钟 · 未结算」，不再使用「实时采集中」。有 settled 值时 settled 永远优先，realtime 只作为 `realtime_estimate` 参考。

## Signal rules / 信号规则（shadow 默认值）

| rule | 口径 | open | escalate | resolve | 其他 |
|---|---|---|---|---|---|
| GMV_DROP | 近 60 分钟增量 vs 同时段中位数 | ≤ −50% | ≤ −70% | ≥ −30% | 基线 ≥ 2000 元/小时 |
| ORDER_DROP | 近 60 分钟增量 | ≤ −50% | ≤ −70% | ≥ −30% | 基线 ≥ 10 单/小时 |
| SPEND_SPIKE | 账户近 60 分钟花费 | ≥ +100% | ≥ +200% | ≤ +50% | 基线 ≥ 200 元/小时 |
| ROI_COLLAPSE | 当日累计 ad_gmv / ad_spend | ≤ −50% | ≤ −70% | ≥ −30% | 花费 ≥ 500 元才评估 |
| CAMPAIGN_SPEND_ANOMALY | 计划当日累计花费 | ≥ +100% | ≥ +200% | ≤ +50% | 基线 ≥ 100 元 |

共同默认：`min_sample = 3` 天、`lookback_days = 14`、`min_duration_minutes = 10`、`cooldown_minutes = 60`、同一 rule/entity/日去重。数据滞后超过 20 分钟或 completeness < 0.5 时不改变信号状态。每个事件包含 `observed / baseline / delta / event_time / evidence_n / completeness / freshness_minutes / provisional`。阈值是 shadow 起点，按 2–4 周复盘调整。

## MiMo queue / 推理队列

- 只有 `SIGNAL_OPEN / SIGNAL_ESCALATE / SIGNAL_RESOLVE / SETTLED_DAILY_BATCH / USER_WHY` 可入队；其他（如 `PAGE_LOAD`、5 分钟 tick）记为 `NO_MIMO_CALL`。
- 输入只允许 `facts`（`verified = true`，realtime 必须 `provisional = true`）、`signals`、扁平 `aggregates`、已脱敏 `cs_evidence`（再次扫描手机号、邮箱、证件号、长编号）。
- `MIMO_INFERENCE_CACHE_V1` key：lane、feature、entity、`rounded_fact_hash`（数值保留 2 位有效数字，比例按 0.05 取整，忽略时间戳与 id）、signal_state、date_window、prompt_version、reported_model_name。未命中原因：`NO_PRIOR_ENTRY / EXPIRED / MODEL_CHANGED / PROMPT_VERSION_CHANGED / SIGNAL_STATE_CHANGED / FACTS_CHANGED`。
- 硬超时：信号解读与 why 60s，daily batch 120s；重试最多 1 次；每个 lane 连续 3 次失败熔断，15 分钟后放 1 个探测请求。
- `DAILY_MIMO_TOKEN_BUDGET = 300000`（所有 lane 合计），优先级带：P0 可用到 100%，P1 90%，P2 80%，P3 60%。超时按预估 token 计费（保守）。
- 页面视图：READY 显示输出；有旧输出时显示「AI解读已过时」；否则「AI解读暂未生成」；排队/运行中给 progress sheet「AI解读 · 后台生成中」（不阻塞「更新完成」）。

## Integration / 接入主看板（主 Agent）

1. 复制 `examples/realtime_config.json`，把现有 settled 库（例如 `sales_forecast.db`）写入 `settled_store_paths`，按真实页面口径改写各指标 `definition`。
2. `python3 -m realtime init --config realtime.json` 创建两个库并登记专用浏览器端口（输出中的 `port`、`profile_dir`、`browser_id` 即登记信息）。
3. `python3 -m realtime browser launch --config realtime.json`，在这个专用窗口里登录商智、京准通各一次；共享 9330 浏览器保持不动。
4. 实现 adapter（`sources.<id>.adapter = "pkg.module:factory"`）：

   ```python
   class ShangzhiRealtime:
       def collect(self, session, slot_ts):
           # session.endpoint 是专用浏览器的 CDP 地址，例如 http://127.0.0.1:9340
           # 遇到登录页 raise realtime.browser.LoginRequired()
           return [{'metric': 'gmv', 'entity': 'store', 'value': '52341.20',
                    'event_time': '2026-09-29T14:34:00+08:00'}]
   ```

   可选 `sources.<id>.login_check = "pkg.module:func"`，`func(endpoint)` 返回 `LOGGED_IN / LOGGED_OUT / UNKNOWN`。
5. 常驻两个进程：`python3 -m realtime run --config realtime.json`（采集循环，自带单实例锁）和 `python3 -m realtime worker --config realtime.json --backend production=pkg.module:mimo_client`（推理 worker；challenger lane 可由 AI客服 用自己的 worker 单独跑，worker 只领取自己有 backend 的 lane）。backend 需实现 `complete(prompt, *, model, max_tokens, timeout)` 并返回 `BackendResponse`。
6. Phase 0-A：把刷新链里的 MiMo 预计算改成 `Step(name, 'inference', fn)`，`fn` 返回 `InferenceRequest('settled_daily_batch', 'SETTLED_DAILY_BATCH', ...)`；沿用现有提示词时传 `prompt=` 和 `prompt_version=`。「更新最新数据」按钮用 `pipeline.catch_up_step()` + 现有事实步骤 + materialize，再接 inference 步骤。
7. 每天 `python3 -m realtime report --config realtime.json --append` 写入 shadow 日志；人工复核信号用 `realtime.signals.review_signal(...)`。
8. Phase 1 前查看 `python3 -m realtime readiness --config realtime.json`（2.6 需 `production_validation` 与非空 build fingerprint）。

先确认现有 MiMo 预计算的日均 token 用量：若 daily batch 本身接近 270k（P1 带上限），需先调整预算，否则 batch 会被预算挡下。

## Shadow report / 观察期日报

`python3 -m realtime report` 输出：`expected_slots / attempted_slots / success_slots / success_rate / p50_lag / p95_lag / browser_login_failure_n / browser_contention_n / signal_open_n / signal_false_positive_review / insufficient_history_n / mimo_call_n / mimo_cache_hit_rate / mimo_p50_latency_ms / mimo_p95_latency_ms / mimo_timeout_n / mimo_token_n / mimo_circuit_open_n`，以及 `fact_refresh_blocked_by_mimo_n / failed_snapshot_erased_good_n / settled_fact_overwrite_n`。

`lag` 定义：每个应采集 slot 的 slot + 2 分钟时刻，页面看到的数据年龄（所有 required 序列 latest good 中最旧的 event_time）。采集失败会直接体现为更大的 lag。

## Simulation / 合成演练

`python3 -m realtime simulate --days 14 --seed 7` 用合成数据、假浏览器、假模型在模拟时钟上跑完整链路，注入采集失败、超时、半页、零值、登录失效、锁争用、MiMo 故障与三类业务异常，并按注入事实自动复核信号。结果只证明机制可用，标记为 `evidence = SIMULATED`，不能替代 14 天真实 shadow。
