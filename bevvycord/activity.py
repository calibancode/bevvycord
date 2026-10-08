"""Small, content-free turn summaries and opt-in local memory diffs."""
from collections import Counter
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
import sqlite3
import uuid

log = logging.getLogger(__name__)
current = ContextVar('activity', default=None)


def usage_line(requests):
    if not requests:
        return 'tokens: unknown'
    parts = []
    for label, key in [('in', 'prompt_tokens'), ('out', 'completion_tokens'),
                       ('hit', 'prompt_cache_hit_tokens'), ('miss', 'prompt_cache_miss_tokens')]:
        values = [r.get(key) for r in requests]
        known = [v for v in values if type(v) is int and v >= 0]
        parts.append(f'{label}={sum(known):,}' + ('+?' if len(known) != len(values) else '') if known else f'{label}=?')
    return 'tokens: ' + ' '.join(parts)


def describe(db, event):
    job, kind = event['job'], event['kind']
    actions, tools = [], []
    if job:
        receipts = db.execute('SELECT name,result FROM tool_receipts WHERE job=? ORDER BY rowid', (job,)).fetchall()
        counts = Counter(name for name, _ in receipts)
        tools = [name + (f'×{count}' if count > 1 else '') for name, count in counts.items()]
        for name, result in receipts:
            if name in ('remember', 'forget') and result:
                try:
                    if 'error' not in json.loads(result):
                        notes = db.execute('SELECT applied FROM memory_notes WHERE job=? AND kind=?', (job, name)).fetchall()
                        status = 'applied' if notes and all(row[0] for row in notes) else 'queued'
                        actions.append(name + ' ' + status)
                except (ValueError, TypeError):
                    pass
        uncertain_tools = sum(result is None for _, result in receipts)
        if uncertain_tools:
            actions.append(f'tool outcome uncertain×{uncertain_tools}')
        delivered = Counter(k for k, state in db.execute('SELECT kind,state FROM deliveries WHERE job=?', (job,)) if state == 'sent')
        if delivered['message']:
            actions.append(f'sent×{delivered["message"]}')
        if delivered['reaction']:
            actions.append(f'reacted×{delivered["reaction"]}')
        if not delivered and event['state'] == 'complete':
            actions.append('private work' if any(n != 'finish' for n in counts) else 'silent')
        uncertain = db.execute("SELECT count(*) FROM deliveries WHERE job=? AND state='started'", (job,)).fetchone()[0]
        if uncertain:
            actions.append(f'delivery uncertain×{uncertain}')
        failed = db.execute("SELECT count(*) FROM deliveries WHERE job=? AND state='failed'", (job,)).fetchone()[0]
        if failed:
            actions.append(f'delivery failed×{failed}')
    elif kind == 'memory':
        changes = json.loads(event['changes'])
        added = sum(line.startswith('+') and not line.startswith('+++') for diff in changes for line in diff.splitlines())
        removed = sum(line.startswith('-') and not line.startswith('---') for diff in changes for line in diff.splitlines())
        actions.append(f'changed +{added}/−{removed} lines' if changes else 'unchanged')
        applied = json.loads(event.get('outputs', '{}')).get('notes_applied', 0)
        if applied:
            actions.append(f'memory requests applied×{applied}')
    else:
        outputs = json.loads(event.get('outputs', '{}'))
        if outputs.get('sent'):
            actions.append(f'sent×{outputs["sent"]}')
        else:
            actions.append('no public output recorded')
    if event['state'] == 'deferred':
        actions.append('new human activity; tool work retained')
    duration = max(0, event['ended'] - event['started']) if event['ended'] is not None else None
    return (f'{kind} · channel {event["channel"]} · {event["state"]}'
            + (f' · {duration:.0f}s' if duration is not None else '')
            + (' · ' + ', '.join(dict.fromkeys(actions)) if actions else '')
            + (' · tools: ' + ', '.join(tools) if tools else '')
            + ' · ' + usage_line(json.loads(event['usage'])))


async def observe(store, channel, kind, operation):
    event = {'id': uuid.uuid4().hex, 'job': None, 'kind': kind, 'channel': str(channel),
             'started': store.clock(), 'ended': None, 'state': 'running', 'usage': [], 'changes': [], 'outputs': {}}
    token = current.set(event)
    store.save_activity(event)
    log.info('%s · %s · channel %s · started · activity %s', store.root.name, kind, channel, event['id'][:8])
    try:
        result = await operation()
        if event['state'] == 'running':
            event['state'] = 'complete'
        return result
    except BaseException as exc:
        import asyncio
        event['state'] = 'cancelled' if isinstance(exc, asyncio.CancelledError) else 'failed'
        raise
    finally:
        event['ended'] = store.clock()
        store.save_activity(event)
        current.reset(token)
        row = dict(event, usage=json.dumps(event['usage']), changes=json.dumps(event['changes']), outputs=json.dumps(event['outputs']))
        try:
            log.info('%s · %s · activity %s', store.root.name, describe(store.db, row), event['id'][:8])
        except sqlite3.Error:
            log.warning('Could not summarize local activity %s', event['id'][:8])


def inspect_activity(path, channels, job=None, limit=50, memory_diffs=False):
    if not path.exists():
        return ['No local activity.']
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        events = []
        allowed = sorted(str(channel) for channel in channels)
        if not allowed:
            return ['No local activity.']
        placeholders = ','.join('?' for _ in allowed)
        if 'activity' in tables:
            query = f'SELECT * FROM activity WHERE channel IN ({placeholders})'
            params = list(allowed)
            if job is not None:
                query += ' AND job=?'
                params.append(job)
            events = [dict(row) for row in db.execute(query + ' ORDER BY started DESC LIMIT ?', [*params, limit])]
        if 'jobs' in tables:
            query = f'SELECT * FROM jobs WHERE channel IN ({placeholders})'
            params = list(allowed)
            if job is not None:
                query += ' AND id=?'
                params.append(job)
            if 'activity' in tables:
                query += ' AND NOT EXISTS (SELECT 1 FROM activity WHERE activity.job=jobs.id)'
            for row in db.execute(query + ' ORDER BY created DESC LIMIT ?', [*params, limit]):
                events.append(dict(id=row['id'], job=row['id'], kind='origin unknown', channel=row['channel'],
                                   started=row['created'], ended=row['updated'], state=row['state'], usage='[]', changes='[]'))
        events.sort(key=lambda event: event['started'], reverse=True)
        lines = []
        for event in events[:limit]:
            date = datetime.fromtimestamp(event['started'], timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            lines.append(f'{date} · {describe(db, event)} · job {event["job"] or "—"}')
            if job:
                for index, request in enumerate(json.loads(event['usage']), 1):
                    lines.append(f'  request {index}: model={request.get("model", "unknown")} · {usage_line([request])}')
                    if request.get('fingerprints'):
                        lines.append('    fingerprints: ' + json.dumps(request['fingerprints'], sort_keys=True))
                for name, result in db.execute('SELECT name,result FROM tool_receipts WHERE job=? ORDER BY rowid', (job,)):
                    state = 'uncertain' if result is None else 'returned'
                    if result:
                        try:
                            if 'error' in json.loads(result):
                                state = 'error'
                        except (ValueError, TypeError):
                            state = 'unreadable receipt'
                    lines.append(f'  {name}: {state}')
                for kind, state in db.execute('SELECT kind,state FROM deliveries WHERE job=? ORDER BY part', (job,)):
                    lines.append(f'  delivery {kind}: {state}')
            if memory_diffs:
                lines.extend(json.loads(event['changes']))
            # Diffs are intentionally excluded from default terminal summaries.
        return lines or ['No local activity.']
