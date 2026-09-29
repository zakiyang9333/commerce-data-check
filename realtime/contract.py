"""Frozen Phase 0 contract for the near-realtime layer (PLAN_B+).

realtime collection -> completeness/freshness -> realtime fact -> deterministic
signal -> inference queue -> inference cache -> dashboard materialization.

Facts and signals are deterministic and never wait for, or fail because of, a
model. Model output is labelled inference, is never executed, and never owns a
business number or decision.
"""

PIPELINE = (
    'realtime_collection',
    'completeness_freshness',
    'realtime_fact',
    'deterministic_signal',
    'inference_queue',
    'inference_cache',
    'dashboard_materialization',
)

FACT_REFRESH_NOT_BLOCKED_BY_MIMO = True
MIMO_FAILURE_DOES_NOT_FAIL_DATA_REFRESH = True
MIMO_OUTPUT = 'MODEL_INFERENCE'
AUTO_EXECUTION = 0

# Phase 0 is shadow only: signals never page anyone and the realtime UI stays off.
SIGNAL_MODE = 'SHADOW'
PRODUCTION_ALERTS_ENABLED = False
REALTIME_UI_ENABLED = False

STORE_KIND = 'REALTIME_ESTIMATE'
REALTIME_BASIS = 'REALTIME_ESTIMATE'
SETTLED_BASIS = 'SETTLED'

# Owned by deterministic code only. Model output carrying these keys is dropped.
MODEL_FORBIDDEN_FIELDS = frozenset({
    'gmv', 'roi', 'fee_rate', 'freshness', 'anomaly_threshold', 'stop_decision', 'budget_execution',
})
EXECUTION_FIELDS = frozenset({'action', 'actions', 'execute', 'auto_execute', 'auto_execution'})

# The only events allowed into the inference queue; everything else is NO_MIMO_CALL.
INFERENCE_TRIGGERS = frozenset({
    'SIGNAL_OPEN', 'SIGNAL_ESCALATE', 'SIGNAL_RESOLVE', 'SETTLED_DAILY_BATCH', 'USER_WHY',
})
PERMITTED_INFERENCE_INPUTS = frozenset({'facts', 'signals', 'aggregates', 'cs_evidence'})

# Priority: P0 signal explanation > settled daily batch > on-demand why > background.
FEATURE_PRIORITY = {
    'signal_explanation': 0,
    'settled_daily_batch': 1,
    'on_demand_why': 2,
    'background_enrichment': 3,
}

# Metric classes that have no trustworthy intraday source and stay T+1 / DAILY_INCREMENTAL.
NOT_REALTIME = (
    ('sku', 'SKU_SETTLED'),
    ('search', 'SEARCH'),
    ('ai_cs', 'AI_CS'),
    ('cs', 'AI_CS'),
    ('customer_service', 'AI_CS'),
    ('price', 'PRICE'),
    ('consumer_intent', 'CONSUMER_INTENT'),
    ('intent', 'CONSUMER_INTENT'),
)

LABEL_MODE = '今日近实时'
LABEL_UNSETTLED = '未结算'
LABEL_SETTLED = '已结算'
LABEL_AI_PENDING = 'AI解读暂未生成'
LABEL_AI_STALE = 'AI解读已过时'
LABEL_AI_RUNNING = 'AI解读 · 后台生成中'
RETIRED_LABELS = ('实时采集中',)


def not_realtime_class(metric, entity_type=''):
    """Return the NOT_REALTIME class for a metric or entity type, else None."""
    for name in (entity_type or '', metric or ''):
        name = name.lower()
        for prefix, cls in NOT_REALTIME:
            if name == prefix or name.startswith(prefix + '_') or name.startswith(prefix + '.'):
                return cls
    return None
