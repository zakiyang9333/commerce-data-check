"""Load and validate the realtime layer configuration."""
import copy
import json
from pathlib import Path

from . import contract, timeutil


class ConfigError(ValueError):
    pass


DEFAULTS = {
    'timezone_offset_hours': 8,
    'paths': {
        'realtime_store': 'data/realtime_fact.db',
        'inference_store': 'data/realtime_inference.db',
        'browser_registry': 'data/realtime_browser.json',
        'browser_lock': 'data/realtime_browser.lock',
        'browser_profile': 'data/realtime_browser_profile',
        'payload': 'data/realtime_payload.json',
        'shadow_log': 'data/realtime_shadow.jsonl',
    },
    # Settled (T+1) stores the realtime writer must never open, e.g. the existing forecast DB.
    'settled_store_paths': [],
    'schedule': {'windows': [
        {'start': '00:00', 'end': '08:00', 'interval_minutes': 30},
        {'start': '08:00', 'end': '24:00', 'interval_minutes': 5},
    ]},
    'store': {
        'retention_days': 42,
        'signal_retention_days': 180,
        'max_future_skew_seconds': 300,
        'max_event_age_hours': 26,
        'busy_timeout_ms': 5000,
    },
    'browser': {
        'host': '127.0.0.1',
        'port_range': [9340, 9399],
        'reserved_ports': [9222, 9330],
        'chrome_path': None,
        'headless': False,
        'extra_args': [],
        'lock_timeout_seconds': 30,
        'startup_timeout_seconds': 20,
        'restart_min_interval_seconds': 60,
        'max_restarts_per_hour': 6,
        'login_url_markers': ['passport.jd.com', '/login'],
    },
    'collector': {'source_timeout_seconds': 90},
    'sources': {},
    'signals': {'max_lag_minutes': 20, 'min_completeness': 0.5, 'rules': []},
    'inference': {
        'daily_token_budget': 300000,
        'priority_budget_share': {'0': 1.0, '1': 0.9, '2': 0.8, '3': 0.6},
        'hard_timeout_seconds': {
            'signal_explanation': 60, 'on_demand_why': 60,
            'settled_daily_batch': 120, 'background_enrichment': 120,
        },
        'max_retries': 1,
        'circuit_failure_threshold': 3,
        'circuit_cooloff_minutes': 15,
        'cache_ttl_hours': {
            'signal_explanation': 24, 'settled_daily_batch': 36,
            'on_demand_why': 6, 'background_enrichment': 48,
        },
        'max_queue_age_minutes': {
            'signal_explanation': 90, 'settled_daily_batch': 720,
            'on_demand_why': 30, 'background_enrichment': 720,
        },
        'max_output_tokens': {
            'signal_explanation': 600, 'settled_daily_batch': 2000,
            'on_demand_why': 800, 'background_enrichment': 800,
        },
        'chars_per_token': 1.5,
        'rounding': {'significant_digits': 2, 'ratio_step': 0.05},
        'prompt_versions': {
            'signal_explanation': 'rt-signal-v1', 'settled_daily_batch': 'settled-batch-v1',
            'on_demand_why': 'why-v1', 'background_enrichment': 'enrich-v1',
        },
        'lanes': {},
    },
}

RULE_DEFAULTS = {
    'GMV_DROP': {'direction': 'drop', 'basis': 'window', 'window_minutes': 60,
                 'open_delta': -0.5, 'escalate_delta': -0.7, 'resolve_delta': -0.3, 'min_baseline': 2000},
    'ORDER_DROP': {'direction': 'drop', 'basis': 'window', 'window_minutes': 60,
                   'open_delta': -0.5, 'escalate_delta': -0.7, 'resolve_delta': -0.3, 'min_baseline': 10},
    'SPEND_SPIKE': {'direction': 'spike', 'basis': 'window', 'window_minutes': 60,
                    'open_delta': 1.0, 'escalate_delta': 2.0, 'resolve_delta': 0.5, 'min_baseline': 200},
    'ROI_COLLAPSE': {'direction': 'drop', 'basis': 'cumulative', 'min_denominator': 500,
                     'open_delta': -0.5, 'escalate_delta': -0.7, 'resolve_delta': -0.3, 'min_baseline': 0.1},
    'CAMPAIGN_SPEND_ANOMALY': {'direction': 'spike', 'basis': 'cumulative',
                               'open_delta': 1.0, 'escalate_delta': 2.0, 'resolve_delta': 0.5, 'min_baseline': 100},
}
RULE_COMMON = {'min_sample': 3, 'lookback_days': 14, 'tolerance_minutes': 30,
               'min_duration_minutes': 10, 'cooldown_minutes': 60}
SINGLETON_ENTITIES = ('store', 'account', 'warehouse')
LANE_ROLES = ('PRODUCTION', 'CHALLENGER')


def _merge(base, override):
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict) and key not in ('sources', 'lanes'):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class Catalog:
    """Per-source metric catalog. Only listed, realtime-eligible metrics may be stored."""

    def __init__(self, sources):
        self._specs, self._required, self.enabled, self.dedicated_browser = {}, {}, {}, {}
        for source, conf in sources.items():
            if not isinstance(conf, dict) or not conf.get('metrics'):
                raise ConfigError('source %s needs a metrics list' % source)
            self.enabled[source] = conf.get('enabled', True) is True
            self.dedicated_browser[source] = conf.get('uses_dedicated_browser', False) is True
            required = []
            for raw in conf['metrics']:
                spec = self._spec(source, raw)
                key = (source, spec['metric'], spec['entity_type'])
                if key in self._specs:
                    raise ConfigError('duplicate metric %s/%s/%s' % key)
                self._specs[key] = spec
                if spec['required']:
                    required.extend((spec['metric'], entity) for entity in spec['entities'])
            self._required[source] = required

    @staticmethod
    def _spec(source, raw):
        metric, entity_type = raw.get('metric'), raw.get('entity_type')
        if not metric or not entity_type:
            raise ConfigError('%s: metric and entity_type are required' % source)
        denied = contract.not_realtime_class(metric, entity_type)
        if denied:
            raise ConfigError('%s: %s/%s is %s and stays T+1/DAILY_INCREMENTAL' % (source, metric, entity_type, denied))
        if not str(raw.get('definition') or '').strip():
            raise ConfigError('%s: %s needs its source definition (口径)' % (source, metric))
        cumulative = raw.get('cumulative', True) is True
        entities = list(raw.get('entities') or [])
        if raw.get('required', False) and not entities:
            if entity_type not in SINGLETON_ENTITIES:
                raise ConfigError('%s: required %s/%s needs an entities list' % (source, metric, entity_type))
            entities = [entity_type]
        for entity in entities:
            if entity != entity_type and not entity.startswith(entity_type + ':'):
                raise ConfigError('%s: entity %s is not of type %s' % (source, entity, entity_type))
        return {
            'metric': metric,
            'entity_type': entity_type,
            'unit': raw.get('unit', ''),
            'definition': raw['definition'].strip(),
            'cumulative': cumulative,
            'required': raw.get('required', False) is True,
            'entities': entities,
            'non_negative': raw.get('non_negative', True) is True,
            'max_drop_ratio': float(raw.get('max_drop_ratio', 0.2)),
            'confirm_n': int(raw.get('confirm_n', 2)),
        }

    def spec(self, source, metric, entity_type):
        return self._specs.get((source, metric, entity_type))

    def required_series(self, source):
        return list(self._required.get(source, ()))

    def sources(self, enabled_only=True):
        return [s for s in self.enabled if self.enabled[s] or not enabled_only]

    def browser_purposes(self):
        return sorted(s for s in self.enabled if self.enabled[s] and self.dedicated_browser[s])


class Config:
    def __init__(self, data, base_dir='.'):
        self.data = data
        self.base_dir = Path(base_dir)
        self.tz = timeutil.business_tz(data['timezone_offset_hours'])
        self.catalog = Catalog(data['sources'])
        self.windows = _windows(data['schedule']['windows'])
        self.rules = [_rule(r, self.catalog) for r in data['signals']['rules']]
        self.lanes = {name: _lane(name, lane) for name, lane in data['inference']['lanes'].items()}
        if sum(1 for lane in self.lanes.values() if lane['serve_to_ui']) > 1:
            raise ConfigError('only one lane may serve the dashboard')
        if data['store']['retention_days'] <= max((r['lookback_days'] for r in self.rules), default=0):
            raise ConfigError('store.retention_days must exceed the longest signal lookback')
        _check_inference(data['inference'])

    def __getitem__(self, key):
        return self.data[key]

    def path(self, name):
        path = Path(self.data['paths'][name])
        return path if path.is_absolute() else self.base_dir / path

    def settled_paths(self):
        return [p if Path(p).is_absolute() else self.base_dir / p for p in self.data['settled_store_paths']]


def _windows(raw):
    windows, cursor = [], 0
    for item in raw:
        start, end = timeutil.parse_hhmm(item['start']), timeutil.parse_hhmm(item['end'])
        interval = int(item['interval_minutes']) * 60
        if start != cursor or end <= start or interval <= 0:
            raise ConfigError('schedule windows must tile the day in order without gaps')
        windows.append((start, end, interval))
        cursor = end
    if cursor != 86400:
        raise ConfigError('schedule windows must end at 24:00')
    return windows


def _series(raw, catalog, rule):
    if not isinstance(raw, dict) or not raw.get('source') or not raw.get('metric'):
        raise ConfigError('%s: series needs source and metric' % rule)
    entity = raw.get('entity')
    entity_type = raw.get('entity_type') or (entity or '').split(':', 1)[0]
    if catalog.spec(raw['source'], raw['metric'], entity_type) is None:
        raise ConfigError('%s: %s/%s/%s is not in the catalog' % (rule, raw['source'], raw['metric'], entity_type))
    return {'source': raw['source'], 'metric': raw['metric'], 'entity': entity, 'entity_type': entity_type}


def _rule(raw, catalog):
    name = raw.get('rule')
    if name not in RULE_DEFAULTS:
        raise ConfigError('unknown rule %r' % name)
    rule = dict(RULE_COMMON, **RULE_DEFAULTS[name])
    rule.update({k: v for k, v in raw.items() if k not in ('series', 'numerator', 'denominator')})
    if 'numerator' in raw or 'denominator' in raw:
        rule['numerator'] = _series(raw.get('numerator'), catalog, name)
        rule['denominator'] = _series(raw.get('denominator'), catalog, name)
        rule['kind'] = 'ratio'
    else:
        rule['series'] = _series(raw.get('series'), catalog, name)
        rule['kind'] = 'value'
    o, e, r = rule['open_delta'], rule['escalate_delta'], rule['resolve_delta']
    if rule['direction'] == 'drop' and not e < o < r:
        raise ConfigError('%s: drop thresholds need escalate < open < resolve (hysteresis)' % name)
    if rule['direction'] == 'spike' and not e > o > r:
        raise ConfigError('%s: spike thresholds need escalate > open > resolve (hysteresis)' % name)
    if rule['basis'] not in ('window', 'cumulative') or (rule['basis'] == 'window' and rule.get('window_minutes', 0) <= 0):
        raise ConfigError('%s: basis must be cumulative or window with window_minutes' % name)
    if rule['kind'] == 'ratio' and rule['basis'] != 'cumulative':
        raise ConfigError('%s: ratio rules compare cumulative values' % name)
    if rule['min_sample'] < 1 or rule['min_baseline'] <= 0:
        raise ConfigError('%s: min_sample and min_baseline must be positive' % name)
    return rule


def _lane(name, raw):
    lane = {
        'name': name,
        'model': raw.get('model') or None,
        'role': raw.get('role', 'CHALLENGER'),
        'features': list(raw.get('features') or []),
        'serve_to_ui': raw.get('serve_to_ui', False) is True,
        'production_validation': raw.get('production_validation', False) is True,
        'build_fingerprint': raw.get('build_fingerprint') or '',
        'backend': raw.get('backend') or {},
        'enabled': raw.get('enabled', True) is True,
    }
    if lane['role'] not in LANE_ROLES:
        raise ConfigError('%s: role must be PRODUCTION or CHALLENGER' % name)
    unknown = set(lane['features']) - set(contract.FEATURE_PRIORITY)
    if unknown:
        raise ConfigError('%s: unknown features %s' % (name, sorted(unknown)))
    if lane['serve_to_ui'] and lane['role'] != 'PRODUCTION':
        raise ConfigError('%s: a challenger lane never serves the dashboard' % name)
    lane['enabled'] = lane['enabled'] and lane['model'] is not None
    return lane


def _check_inference(conf):
    if conf['daily_token_budget'] <= 0 or conf['max_retries'] < 0 or conf['circuit_failure_threshold'] < 1:
        raise ConfigError('inference budget, retries and circuit threshold must be positive')
    shares = conf['priority_budget_share']
    for priority in contract.FEATURE_PRIORITY.values():
        if not 0 < float(shares[str(priority)]) <= 1:
            raise ConfigError('priority_budget_share must be in (0, 1]')
    for feature in contract.FEATURE_PRIORITY:
        for key in ('hard_timeout_seconds', 'cache_ttl_hours', 'max_queue_age_minutes', 'max_output_tokens', 'prompt_versions'):
            if feature not in conf[key]:
                raise ConfigError('inference.%s missing %s' % (key, feature))


def load(source, base_dir=None):
    """Load a config from a JSON path or a dict; relative paths resolve against base_dir."""
    if isinstance(source, dict):
        raw, base = source, Path(base_dir or '.')
    else:
        path = Path(source)
        raw, base = json.loads(path.read_text(encoding='utf-8')), Path(base_dir or path.parent)
    return Config(_merge(DEFAULTS, raw), base)
