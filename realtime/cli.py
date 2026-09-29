"""Command line for the Phase 0 realtime layer: python3 -m realtime <command> --config FILE."""
import argparse
import importlib
import json
import signal
import sys
import threading
import time

from . import config as config_module
from . import simulate, timeutil
from .browser import DedicatedBrowser, FileLock
from .collector import Collector
from .inference import InferenceQueue, InferenceStore, InferenceWorker, backends_from_config, promotion_readiness
from .pipeline import RealtimePipeline
from .schedule import Schedule
from .shadow import append_log, daily_report, public, summarize
from .signals import SignalEngine
from .store import RealtimeFactStore


def load_object(spec):
    """'package.module:attribute' -> the attribute; a factory without collect()/complete() is called."""
    module, _, name = spec.partition(':')
    try:
        target = getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError) as exc:
        raise SystemExit('cannot load %s: %s' % (spec, exc))
    return target() if callable(target) and not hasattr(target, 'collect') and not hasattr(target, 'complete') else target


def _print(data):
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def _browser(cfg):
    checks = {s: load_object(c['login_check']) for s, c in cfg['sources'].items() if c.get('login_check')}
    return DedicatedBrowser.from_config(cfg, login_checks=checks)


def _pipeline(cfg, collect=True):
    """Full tick pipeline; with collect=False no browser or adapters are loaded (read-only uses)."""
    store = RealtimeFactStore.from_config(cfg)
    browser = _browser(cfg) if collect and cfg.catalog.browser_purposes() else None
    adapters = {s: load_object(c['adapter']) for s, c in cfg['sources'].items()
                if collect and c.get('adapter') and s in cfg.catalog.sources()}
    queue = InferenceQueue.from_config(cfg, InferenceStore(cfg.path('inference_store')))
    ui_lane = next((name for name, lane in cfg.lanes.items() if lane['serve_to_ui']), None)
    return RealtimePipeline(cfg, store, Collector.from_config(cfg, store, adapters, browser=browser),
                            SignalEngine.from_config(cfg, store), queue, Schedule(cfg.windows, cfg.tz),
                            browser=browser, payload_path=cfg.path('payload'), ui_lane=ui_lane)


def cmd_init(cfg, args):
    RealtimeFactStore.from_config(cfg)
    InferenceStore(cfg.path('inference_store'))
    out = {'realtime_store': str(cfg.path('realtime_store')), 'inference_store': str(cfg.path('inference_store'))}
    if cfg.catalog.browser_purposes():
        out['dedicated_browser'] = _browser(cfg).register()
    _print(out)


def cmd_browser(cfg, args):
    browser = _browser(cfg)
    action = {'register': browser.register, 'launch': browser.launch, 'probe': browser.probe,
              'ensure': browser.ensure_running, 'stop': browser.stop}[args.action]
    _print(action())


def cmd_collect(cfg, args):
    result = _pipeline(cfg).tick()
    _print({k: v for k, v in result.items() if k != 'payload'})


def cmd_run(cfg, args):
    loop_lock = FileLock(cfg.path('realtime_store').with_name('realtime_collector.lock'))
    if not loop_lock.acquire(0, holder='collector-loop'):
        print('another realtime collector loop is running', file=sys.stderr)
        return 2
    pipeline, stop = _pipeline(cfg), threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    last_day = None
    try:
        while not stop.is_set():
            now = time.time()
            stop.wait(max(0.0, pipeline.schedule.next_slot(int(now)) - now))
            if stop.is_set():
                break
            day = timeutil.business_date(int(time.time()), cfg.tz)
            if day != last_day:
                pipeline.store.apply_retention(day, cfg['store']['retention_days'], cfg['store']['signal_retention_days'])
                last_day = day
            result = pipeline.tick()
            print(json.dumps({'slot': timeutil.iso(result['slot_ts'], cfg.tz),
                              'attempts': {a['source']: a['status'] for a in result['attempts']},
                              'signals': len(result['signal_events'])}, ensure_ascii=False), flush=True)
    finally:
        loop_lock.release()
    return 0


def cmd_worker(cfg, args):
    backends = backends_from_config(cfg)
    for item in args.backend or []:
        lane, _, spec = item.partition('=')
        backends[lane] = load_object(spec)
    worker = InferenceWorker.from_config(cfg, InferenceStore(cfg.path('inference_store')), backends)
    if args.once:
        _print(worker.drain())
        return 0
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    worker.run_forever(stop)
    return 0


def cmd_payload(cfg, args):
    _print(_pipeline(cfg, collect=False).materialize())


def cmd_report(cfg, args):
    store = RealtimeFactStore.from_config(cfg)
    istore = InferenceStore(cfg.path('inference_store'))
    day = args.date or timeutil.business_date(int(time.time()), cfg.tz)
    report = daily_report(cfg, store, Schedule(cfg.windows, cfg.tz), day, inference_store=istore,
                          until_ts=min(int(time.time()), timeutil.day_start(timeutil.shift_day(day, 1), cfg.tz)))
    if args.append:
        append_log(cfg.path('shadow_log'), report)
    _print(public(report))


def cmd_summary(cfg, args):
    store = RealtimeFactStore.from_config(cfg)
    istore = InferenceStore(cfg.path('inference_store'))
    schedule, day, reports = Schedule(cfg.windows, cfg.tz), args.start, []
    while day <= args.end:
        reports.append(daily_report(cfg, store, schedule, day, inference_store=istore))
        day = timeutil.shift_day(day, 1)
    _print(summarize(reports, istore))


def cmd_retention(cfg, args):
    store = RealtimeFactStore.from_config(cfg)
    _print(store.apply_retention(timeutil.business_date(int(time.time()), cfg.tz), cfg['store']['retention_days'],
                                 cfg['store']['signal_retention_days']))


def cmd_readiness(cfg, args):
    istore = InferenceStore(cfg.path('inference_store'))
    _print([promotion_readiness(istore, lane) for lane in cfg.lanes.values() if lane['role'] == 'CHALLENGER'])


COMMANDS = {'init': cmd_init, 'browser': cmd_browser, 'collect': cmd_collect, 'run': cmd_run, 'worker': cmd_worker,
            'payload': cmd_payload, 'report': cmd_report, 'summary': cmd_summary, 'retention': cmd_retention,
            'readiness': cmd_readiness}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ['simulate']:
        return simulate.main(argv[1:])
    parser = argparse.ArgumentParser(prog='python3 -m realtime', description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in COMMANDS:
        command = sub.add_parser(name)
        command.add_argument('--config', required=True)
        command.add_argument('--base-dir', default='.', help='directory that relative paths resolve against')
        if name == 'browser':
            command.add_argument('action', choices=['register', 'launch', 'probe', 'ensure', 'stop'])
        if name == 'worker':
            command.add_argument('--backend', action='append', help='lane=package.module:factory')
            command.add_argument('--once', action='store_true')
        if name == 'report':
            command.add_argument('--date')
            command.add_argument('--append', action='store_true')
        if name == 'summary':
            command.add_argument('--start', required=True)
            command.add_argument('--end', required=True)
    sub.add_parser('simulate', help='synthetic shadow run (SIMULATED evidence); see simulate --help')
    args = parser.parse_args(argv)
    cfg = config_module.load(args.config, base_dir=args.base_dir)
    return COMMANDS[args.command](cfg, args) or 0
