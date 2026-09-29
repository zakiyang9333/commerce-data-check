"""Phase 0-A: a refresh finishes on facts; model work is queued, never awaited.

A chain is an ordered list of steps. 'fact' and 'materialize' steps run in their
declared order and alone decide fact_refresh / dashboard_facts; a step that raises
stops the chain, and a step may return {'state': 'PARTIAL'} to report a degraded but
usable result. 'inference' steps only build InferenceRequests; they are submitted after
every fact step has finished, and a queue failure is reported as inference_status,
never as a refresh failure.
"""
import time
import uuid
from collections import namedtuple

from . import contract, timeutil

Step = namedtuple('Step', 'name kind fn')
STEP_KINDS = ('fact', 'materialize', 'inference')
# Submitting is a local database write; anything slower means someone waited on a model.
BLOCKED_THRESHOLD_MS = 1000
_STATUS_ORDER = ('PENDING', 'FAILED', 'SKIPPED_BUDGET', 'READY', 'NO_MIMO_CALL')
_STATUS_MAP = {'PENDING': 'PENDING', 'RUNNING': 'PENDING', 'CACHED': 'READY', 'NO_MIMO_CALL': 'NO_MIMO_CALL',
               'SKIPPED_BUDGET': 'SKIPPED_BUDGET', 'FAILED': 'FAILED'}


def summarize_inference(statuses):
    mapped = {_STATUS_MAP.get(s, 'FAILED') for s in statuses}
    for status in _STATUS_ORDER:
        if status in mapped:
            return status
    return 'NOT_REQUESTED'


def _submit(steps, queue):
    statuses = []
    for step in steps:
        try:
            for request in step.fn() or ():
                statuses.extend(result['status'] for result in queue.submit(request))
        except Exception:
            statuses.append('FAILED')
    return summarize_inference(statuses) if steps else 'NOT_REQUESTED'


def progress_sheet(run):
    rows = [{'label': s['name'], 'state': s['state']} for s in run['steps']]
    rows.append({'label': '更新完成' if run['update_complete'] else '更新失败',
                 'state': 'DONE' if run['update_complete'] else 'FAILED'})
    if run['inference_status'] == 'PENDING':
        rows.append({'label': contract.LABEL_AI_RUNNING, 'state': 'BACKGROUND', 'blocking': False})
    return rows


def run_chain(steps, *, queue=None, store=None, tz=None, clock=time.time, kind='manual'):
    """Run a refresh chain; returns fact_refresh, dashboard_facts, inference_status and progress rows."""
    unknown = [s.name for s in steps if s.kind not in STEP_KINDS]
    if unknown:
        raise ValueError('unknown step kinds: %s' % unknown)
    run = {'run_id': uuid.uuid4().hex, 'kind': kind, 'started_ts': clock(), 'facts_ready_ts': None,
           'fact_refresh': 'PASS', 'dashboard_facts': 'AVAILABLE', 'inference_status': 'NOT_REQUESTED',
           'inference_wait_ms': 0.0, 'failed_step': None, 'steps': []}
    deferred = [s for s in steps if s.kind == 'inference']
    for step in steps:
        if step.kind == 'inference':
            continue
        started = clock()
        try:
            outcome = step.fn()
        except Exception as exc:
            run.update(fact_refresh='FAIL', dashboard_facts='PREVIOUS', failed_step=step.name, error=type(exc).__name__)
            run['steps'].append({'name': step.name, 'kind': step.kind, 'state': 'FAILED'})
            break
        state = outcome.get('state', 'DONE') if isinstance(outcome, dict) else 'DONE'
        run['steps'].append({'name': step.name, 'kind': step.kind, 'state': state, 'seconds': round(clock() - started, 3)})
    else:
        run['facts_ready_ts'] = clock()
        started = clock()
        if deferred and queue is None:
            run['inference_status'] = 'FAILED'
        else:
            run['inference_status'] = _submit(deferred, queue)
        run['inference_wait_ms'] = (clock() - started) * 1000
    run['completed_ts'] = clock()
    run['blocked_by_inference'] = int(run['inference_wait_ms'] > BLOCKED_THRESHOLD_MS)
    run['update_complete'] = run['fact_refresh'] == 'PASS'
    run['progress'] = progress_sheet(run)
    if store is not None:
        store.log_refresh(dict(run, business_date=timeutil.business_date(int(run['started_ts']), tz)))
    return run
