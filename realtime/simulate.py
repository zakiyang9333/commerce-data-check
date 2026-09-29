"""Synthetic Phase 0 shadow run with fault injection. No network, no real platforms.

Runs the real store, collector, signal engine, inference queue/worker, refresh chain and
payload against synthetic sources, a fake browser and a fake model on a simulated clock,
then checks the Phase 0 invariants. Every number it reports is SIMULATED evidence of the
mechanics, not production evidence.
"""
import contextlib
import json
import math
import random
import sqlite3
from pathlib import Path

from . import config as config_module
from . import contract, timeutil
from .browser import BrowserContention, LoginRequired
from .collector import Collector
from .inference import (BackendResponse, InferenceQueue, InferenceRequest, InferenceStore, InferenceTimeout,
                        InferenceWorker, promotion_readiness)
from .payload import build_realtime_payload
from .pipeline import RealtimePipeline
from .refresh import Step, run_chain
from .schedule import Schedule
from .shadow import daily_report, public, summarize
from .signals import SignalEngine, review_signal
from .store import RealtimeFactStore, StoreGuardError

EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / 'examples' / 'realtime_config.json'
STORE_SOURCE, ADS_SOURCE = 'jd_shangzhi_realtime', 'jd_jingzhuntong_intraday'
SHADOW_LANE = 'MIMO26_REALTIME_SHADOW_V1'
CAMPAIGN_SHARE = {'campaign:c1': 0.5, 'campaign:c2': 0.3, 'campaign:c3': 0.2}
# Relative sales weight per hour of day.
HOURLY = [0.6, 0.35, 0.2, 0.15, 0.15, 0.25, 0.6, 1.2, 2.2, 3.4, 4.6, 5.0,
          4.4, 4.2, 4.6, 4.8, 4.6, 4.2, 4.4, 5.2, 6.4, 7.2, 6.0, 3.2]
WEEKDAY = [1.0, 0.97, 0.98, 1.0, 1.05, 1.12, 1.08]


class SimClock:
    """Simulated wall clock; also usable as a plain callable."""

    def __init__(self, start):
        self.t, self.m = float(start), 0.0

    def __call__(self):
        return self.t

    def time(self):
        return self.t

    def monotonic(self):
        return self.m

    def advance(self, seconds):
        self.t += seconds
        self.m += seconds

    def set(self, ts):
        if ts > self.t:
            self.advance(ts - self.t)


def _share(tod_s):
    """Cumulative share of daily sales by time of day (piecewise linear in the hour)."""
    total = sum(HOURLY)
    hour, rest = divmod(max(0, min(tod_s, 86400)), 3600)
    hour = int(hour)
    done = sum(HOURLY[:hour]) + (HOURLY[hour] * rest / 3600 if hour < 24 else 0)
    return done / total


class Market:
    """Deterministic synthetic store and ads curves with scheduled incidents."""

    def __init__(self, tz, seed, incidents):
        self.tz, self.seed, self.incidents = tz, seed, incidents
        self._days = {}

    def day(self, day):
        if day not in self._days:
            rng = random.Random('%s|market|%s' % (self.seed, day))
            factor = WEEKDAY[timeutil.weekday(day)]
            self._days[day] = {
                'gmv': 150000 * factor * rng.lognormvariate(0, 0.06),
                'aov': 180 * rng.lognormvariate(0, 0.03),
                'cvr': 0.045 * rng.lognormvariate(0, 0.05),
                'spend': 15000 * rng.lognormvariate(0, 0.05),
                'roi': 4.0 * rng.lognormvariate(0, 0.06),
            }
        return self._days[day]

    def _incident(self, kind, day):
        return [(s, e, f) for k, d, s, e, f in self.incidents if k == kind and d == day]

    @staticmethod
    def _cut(curve, tod, windows):
        """Cumulative curve with the volume inside each (start, end, factor) window scaled."""
        value = curve(tod)
        for start, end, factor in windows:
            if tod > start:
                value -= (1 - factor) * (curve(min(tod, end)) - curve(start))
        return value

    def store_values(self, day, tod):
        d = self.day(day)
        share = self._cut(_share, tod, self._incident('sales_outage', day))
        gmv = d['gmv'] * share
        visitors_share = 0.8 * share + 0.2 * min(tod, 86400) / 86400
        return {'gmv': round(gmv, 2), 'orders': int(gmv / d['aov']), 'visitors': int(d['gmv'] / d['aov'] / d['cvr'] * visitors_share)}

    def ads_values(self, day, tod):
        d = self.day(day)
        spikes, collapse = self._incident('spend_spike', day), self._incident('roi_collapse', day)
        out, spend_total, gmv_total = {}, 0.0, 0.0
        for campaign, share in CAMPAIGN_SHARE.items():
            windows = spikes if campaign == 'campaign:c2' else []

            def spend_at(t, share=share, windows=windows):
                return d['spend'] * share * self._cut(_pacing, t, windows)

            # Attributed order value lags spend by about 30 minutes; a tracking collapse scales it down.
            attributed_to = max(tod - 1800, 0)
            attributed = d['roi'] * spend_at(attributed_to)
            for start, end, factor in collapse:
                if attributed_to > start:
                    attributed -= (1 - factor) * d['roi'] * (spend_at(min(attributed_to, end)) - spend_at(start))
            spend = spend_at(tod)
            out['ad_spend', campaign], out['ad_gmv', campaign] = round(spend, 2), round(attributed, 2)
            spend_total, gmv_total = spend_total + spend, gmv_total + attributed
        out['ad_spend', 'account'], out['ad_gmv', 'account'] = round(spend_total, 2), round(gmv_total, 2)
        out['ad_orders', 'account'] = int(gmv_total / d['aov'])
        out['ad_clicks', 'account'] = int(spend_total / 0.9)
        return out


def _pacing(tod):
    """Ad spend follows budget pacing, flatter than sales."""
    return 0.7 * _share(tod) + 0.3 * min(max(tod, 0), 86400) / 86400


class Faults:
    """Deterministic per-slot fault draws plus scheduled logouts and model outages."""

    RATES = (('error', 0.02), ('timeout', 0.01), ('partial', 0.03), ('zero', 0.005))

    def __init__(self, seed, tz, logouts, contention_rate=0.005):
        self.seed, self.tz, self.logouts, self.contention_rate = seed, tz, logouts, contention_rate

    def draw(self, source, slot_ts):
        roll = random.Random('%s|fault|%s|%s' % (self.seed, source, slot_ts)).random()
        edge = 0.0
        for name, rate in self.RATES:
            edge += rate
            if roll < edge:
                return name
        return None

    def logged_out(self, ts):
        day = timeutil.business_date(int(ts), self.tz)
        tod = int(ts) - timeutil.day_start(day, self.tz)
        return any(d == day and s <= tod < e for d, s, e in self.logouts)

    def contention(self, purpose, ts):
        return random.Random('%s|lock|%s|%s' % (self.seed, purpose, int(ts) // 60)).random() < self.contention_rate


class SimBrowser:
    def __init__(self, faults, clock, purposes):
        self.faults, self.clock, self.purposes = faults, clock, purposes
        self.contention_n = 0

    @contextlib.contextmanager
    def session(self, purpose, timeout=30):
        if self.faults.contention(purpose, self.clock()):
            self.contention_n += 1
            raise BrowserContention('held by another job')
        yield SimSession(self, purpose)

    def ensure_running(self):
        return {'action': 'NONE'}


class SimSession:
    browser_id = 'sim-dedicated-browser'

    def __init__(self, browser, purpose):
        self.browser, self.purpose = browser, purpose

    def health(self):
        return {'status': 'LOGIN_REQUIRED' if self.browser.faults.logged_out(self.browser.clock()) else 'HEALTHY'}


class SimAdapter:
    def __init__(self, source, market, faults, clock, tz):
        self.source, self.market, self.faults, self.clock, self.tz = source, market, faults, clock, tz

    def collect(self, session, slot_ts):
        fault = self.faults.draw(self.source, slot_ts)
        if fault == 'error':
            raise RuntimeError('page script error')
        if fault == 'timeout':
            raise TimeoutError('page load timeout')
        if self.faults.logged_out(self.clock()):
            raise LoginRequired()
        event_ts = int(self.clock()) // 60 * 60 - 60  # platform shows data up to the previous minute
        day = timeutil.business_date(event_ts, self.tz)
        tod = event_ts - timeutil.day_start(day, self.tz)
        if self.source == STORE_SOURCE:
            values = {(k, 'store'): v for k, v in self.market.store_values(day, tod).items()}
            dropped = ('visitors', 'store')
        else:
            values = self.market.ads_values(day, tod)
            dropped = ('ad_gmv', 'account')
        if fault == 'partial':
            values.pop(dropped)
        if fault == 'zero':
            first = sorted(values)[0]
            values[first] = 0
        return [{'metric': m, 'entity': e, 'value': v, 'event_time': event_ts} for (m, e), v in sorted(values.items())]


class SimModel:
    """Fake MiMo backend: lognormal latency, occasional very slow calls, scheduled outages."""

    def __init__(self, clock, seed, outages, tz, fingerprint=None):
        self.clock, self.rng, self.outages, self.tz, self.fingerprint = clock, random.Random(seed), outages, tz, fingerprint
        self.calls = 0

    def complete(self, prompt, *, model, max_tokens, timeout):
        self.calls += 1
        now = self.clock.time()
        day = timeutil.business_date(int(now), self.tz)
        tod = now - timeutil.day_start(day, self.tz)
        if any(d == day and s <= tod < e for d, s, e in self.outages):
            self.clock.advance(min(2.0, timeout))
            raise ConnectionError('upstream 502')
        latency = self.rng.uniform(70, 150) if self.rng.random() < 0.03 else self.rng.lognormvariate(math.log(12), 0.55)
        if latency > timeout:
            self.clock.advance(timeout)
            raise InferenceTimeout('slow upstream')
        self.clock.advance(latency)
        completion = self.rng.randint(150, 400)
        text = json.dumps({'summary': '可能原因待核查（模拟输出）', 'hypotheses': ['流量或投放变化'], 'checks': ['核对商智与京准通明细']},
                          ensure_ascii=False)
        return BackendResponse(text=text, reported_model_name=model, prompt_tokens=len(prompt) // 2,
                               completion_tokens=completion, total_tokens=len(prompt) // 2 + completion,
                               fingerprint=self.fingerprint)


def _settled_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute('CREATE TABLE IF NOT EXISTS settled_daily (business_date TEXT, metric TEXT, entity TEXT, value TEXT, '
                 'PRIMARY KEY (business_date, metric, entity))')
    conn.commit()
    conn.close()


def _write_settled(path, day, values):
    conn = sqlite3.connect(str(path))
    with conn:
        for (metric, entity), value in values.items():
            conn.execute('INSERT OR REPLACE INTO settled_daily VALUES (?, ?, ?, ?)', (day, metric, entity, str(value)))
    conn.close()


def _read_settled(path, day):
    conn = sqlite3.connect(str(path))
    rows = conn.execute('SELECT metric, entity, value FROM settled_daily WHERE business_date = ?', (day,)).fetchall()
    conn.close()
    return {(m, e): v for m, e, v in rows}


RULE_INCIDENT = {'GMV_DROP': 'sales_outage', 'ORDER_DROP': 'sales_outage', 'SPEND_SPIKE': 'spend_spike',
                 'CAMPAIGN_SPEND_ANOMALY': 'spend_spike', 'ROI_COLLAPSE': 'roi_collapse'}


def _review_against_ground_truth(store, incidents, now_ts):
    """Stand-in for the human review: a signal is a true positive only on a day with its injected incident."""
    with store.read() as conn:
        opened = conn.execute("SELECT signal_id, rule, business_date FROM signal_event WHERE transition = 'OPEN'").fetchall()
    for row in opened:
        hit = any(kind == RULE_INCIDENT[row['rule']] and day == row['business_date'] for kind, day, _, _, _ in incidents)
        review_signal(store, row['signal_id'], 'TRUE_POSITIVE' if hit else 'FALSE_POSITIVE', now_ts,
                      reviewer='simulation-ground-truth')


def sim_config(workdir, shadow_model='simulated-mimo-2.6'):
    raw = json.loads(EXAMPLE_CONFIG.read_text(encoding='utf-8'))
    raw['inference']['lanes'][SHADOW_LANE]['model'] = shadow_model
    for source in raw['sources'].values():
        source.pop('adapter', None)
    return config_module.load(raw, base_dir=workdir)


def run(workdir, days=14, seed=7, first_day='2026-09-01', incidents=None, logouts=None, model_outages=None,
        page_load_every=3, progress=None):
    """Run a synthetic shadow period; return (summary, daily reports, checks)."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    cfg = sim_config(workdir)
    tz = cfg.tz
    day_list = [timeutil.shift_day(first_day, i) for i in range(days)]
    h = 3600
    incidents = incidents if incidents is not None else [
        ('roi_collapse', day_list[min(8, days - 1)], 9 * h, 24 * h, 0.3),
        ('sales_outage', day_list[min(10, days - 1)], 14 * h, 16 * h, 0.1),
        ('spend_spike', day_list[min(12, days - 1)], 9 * h + 1800, 12 * h + 1800, 6.0),
    ]
    logouts = logouts if logouts is not None else [(day_list[min(4, days - 1)], 13 * h, 13 * h + 45 * 60)]
    # Covers the 09:00 settled batch and the 11:00 button refresh, so the breaker must open.
    model_outages = model_outages if model_outages is not None else [(day_list[min(6, days - 1)], 8 * h + 1800, 12 * h + 1800)]
    clock = SimClock(timeutil.day_start(first_day, tz))
    settled_path = cfg.settled_paths()[0]
    settled_path.parent.mkdir(parents=True, exist_ok=True)
    _settled_db(settled_path)
    checks = {'settled_store_guard': 'NOT_TESTED'}
    try:
        RealtimeFactStore(settled_path, cfg.catalog, tz, settled_paths=cfg.settled_paths())
    except StoreGuardError:
        checks['settled_store_guard'] = 'REFUSED'
    market = Market(tz, seed, incidents)
    faults = Faults(seed, tz, logouts)
    store = RealtimeFactStore.from_config(cfg)
    istore = InferenceStore(cfg.path('inference_store'))
    browser = SimBrowser(faults, clock, cfg.catalog.browser_purposes())
    adapters = {s: SimAdapter(s, market, faults, clock, tz) for s in (STORE_SOURCE, ADS_SOURCE)}
    collector = Collector.from_config(cfg, store, adapters, browser=browser, clock=clock)
    engine = SignalEngine.from_config(cfg, store)
    queue = InferenceQueue.from_config(cfg, istore, clock)
    backends = {'production': SimModel(clock, '%s|prod' % seed, model_outages, tz, fingerprint='sim-prod-build'),
                SHADOW_LANE: SimModel(clock, '%s|shadow' % seed, model_outages, tz)}
    worker = InferenceWorker.from_config(cfg, istore, backends, clock)
    schedule = Schedule(cfg.windows, tz)
    pipeline = RealtimePipeline(cfg, store, collector, engine, queue, schedule, browser=browser,
                                payload_path=cfg.path('payload'), ui_lane='production', clock=clock)
    expected_settled, page_loads, page_load_jobs = {}, 0, 0

    def job_count():
        with istore.read() as conn:
            return conn.execute('SELECT COUNT(*) FROM inference_job').fetchone()[0]

    def settled_values(day):
        tod = 86400 - 1
        values = {(k, 'store'): v for k, v in market.store_values(day, tod).items()}
        ads = market.ads_values(day, tod + 1800)
        values.update({k: v for k, v in ads.items() if k[1] == 'account'})
        return values

    def batch_request(day):
        facts = [{'metric': m, 'entity': e, 'value': float(v), 'basis': contract.SETTLED_BASIS, 'verified': True}
                 for (m, e), v in sorted(_read_settled(settled_path, day).items())]
        return [InferenceRequest('settled_daily_batch', 'SETTLED_DAILY_BATCH', 'store', day, {'facts': facts})]

    for index, day in enumerate(day_list):
        yesterday = timeutil.shift_day(day, -1)
        slots = schedule.slots(day)
        for n, slot in enumerate(slots):
            clock.set(slot + 20)
            tod = slot - timeutil.day_start(day, tz)
            if tod == 9 * 3600 and index > 0:
                values = settled_values(yesterday)
                expected_settled[yesterday] = {k: str(v) for k, v in values.items()}
                run_chain([Step('T+1事实', 'fact', lambda d=yesterday, v=values: _write_settled(settled_path, d, v)),
                           Step('MiMo预计算', 'inference', lambda d=yesterday: batch_request(d)),
                           Step('看板物化', 'materialize', pipeline.materialize)],
                          queue=queue, store=store, tz=tz, clock=clock, kind='settled_daily')
            if tod in (11 * 3600, 15 * 3600, 20 * 3600 + 1800) and index > 0:
                run_chain([pipeline.catch_up_step(), Step('看板刷新', 'materialize', pipeline.materialize),
                           Step('AI解读', 'inference', lambda d=yesterday: batch_request(d))],
                          queue=queue, store=store, tz=tz, clock=clock, kind='button')
            else:
                pipeline.tick(slot)
            if tod in (14 * 3600 + 1800, 21 * 3600):
                facts = [{'metric': o['metric'], 'entity': o['entity'], 'value': float(o['value']),
                          'basis': contract.REALTIME_BASIS, 'provisional': True, 'verified': True}
                         for o in store.good_snapshot(day).values() if o['entity'] in ('store', 'account')]
                queue.submit(InferenceRequest('on_demand_why', 'USER_WHY', 'store', day, {'facts': facts}))
            if n % page_load_every == 0:
                before = job_count()
                build_realtime_payload(store, cfg.catalog, tz, int(clock()), inference=queue.view('production', 'settled_daily_batch',
                                                                                                    'store', yesterday))
                queue.submit(InferenceRequest('on_demand_why', 'PAGE_LOAD', 'store', day, {}))
                page_loads += 1
                page_load_jobs += job_count() - before
            next_slot = slots[n + 1] if n + 1 < len(slots) else timeutil.day_start(timeutil.shift_day(day, 1), tz)
            while clock() < next_slot and worker.run_once() is not None:
                pass
        store.apply_retention(day, cfg['store']['retention_days'], cfg['store']['signal_retention_days'])
        if progress:
            progress(day)

    end_ts = int(clock())
    _review_against_ground_truth(store, incidents, end_ts)
    reports = []
    for day in day_list:
        settled = {k: v for k, v in _read_settled(settled_path, day).items()} or None
        reports.append(daily_report(cfg, store, schedule, day, inference_store=istore, until_ts=min(
            end_ts, timeutil.day_start(timeutil.shift_day(day, 1), tz)), settled=settled))
    mismatched = sum(1 for day, values in expected_settled.items()
                     for key, value in values.items() if _read_settled(settled_path, day).get(key) != value)
    checks.update({
        'settled_rows_changed_by_realtime': mismatched,
        'page_loads': page_loads,
        'page_load_mimo_jobs': page_load_jobs,
        'retired_label_used': any(label in json.dumps(pipeline.materialize(end_ts), ensure_ascii=False)
                                  for label in contract.RETIRED_LABELS),
        'shadow_lane_readiness': promotion_readiness(istore, cfg.lanes[SHADOW_LANE]),
    })
    summary = summarize(reports, istore)
    summary['settled_fact_overwrite_n'] += mismatched
    summary['evidence'] = 'SIMULATED'
    return summary, [public(r) for r in reports], checks


def main(argv=None):
    import argparse
    import tempfile
    parser = argparse.ArgumentParser(prog='python3 -m realtime simulate',
                                     description='Synthetic Phase 0 shadow run (SIMULATED evidence).')
    parser.add_argument('--days', type=int, default=14)
    parser.add_argument('--seed', type=int, default=7)
    parser.add_argument('--workdir')
    parser.add_argument('--daily', action='store_true', help='print every daily report')
    args = parser.parse_args(argv)
    workdir = args.workdir or tempfile.mkdtemp(prefix='realtime-sim-')
    summary, reports, checks = run(workdir, days=args.days, seed=args.seed)
    out = {'summary': summary, 'checks': checks, 'workdir': str(workdir)}
    if args.daily:
        out['daily'] = reports
    print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
    return 0



if __name__ == '__main__':
    raise SystemExit(main())
