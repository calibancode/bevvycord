import asyncio
import json
import logging

import httpx
import pytest

from bevvycord.activity import current, inspect_activity, observe
from bevvycord.provider import Provider
from bevvycord.storage import Store
from test_runtime import job


def test_silent_private_turn_and_receipts_are_content_free(tmp_path, caplog):
    current_job, _ = job(tmp_path)
    store = current_job.store
    async def work():
        event = current.get()
        event['job'] = current_job.id
        store.receipt_start(current_job.id, 'one', 'exec', 'SECRET COMMAND')
        store.receipt_finish(current_job.id, 'one', '{"stdout":"PRIVATE CONTENT"}')
        store.receipt_start(current_job.id, 'two', 'remember', 'PRIVATE NOTE')
        store.receipt_finish(current_job.id, 'two', '{"status":"queued"}')
    with caplog.at_level(logging.INFO):
        asyncio.run(observe(store, 100, 'check-in', work))
    lines = '\n'.join(inspect_activity(store.root / 'history.sqlite3', {100}, current_job.id))
    assert 'private work' in lines and 'remember queued' in lines
    assert 'exec: returned' in lines and 'check-in' in lines
    for secret in ['SECRET COMMAND', 'PRIVATE CONTENT', 'PRIVATE NOTE']:
        assert secret not in lines and secret not in caplog.text
    assert current.get() is None


def test_memory_diff_is_opt_in_and_unchanged_runs_have_no_diff(tmp_path):
    store = Store(tmp_path, 'rowan')
    async def write(text):
        store.write_memory(100, text, 0)
    asyncio.run(observe(store, 100, 'memory', lambda: write('- personal memory')))
    asyncio.run(observe(store, 100, 'memory', lambda: write('- personal memory')))
    default = '\n'.join(inspect_activity(store.root / 'history.sqlite3', {100}))
    assert 'personal memory' not in default
    assert 'changed +1/−0 lines' in default and 'unchanged' in default
    detailed = '\n'.join(inspect_activity(store.root / 'history.sqlite3', {100}, memory_diffs=True))
    assert '+- personal memory' in detailed
    assert inspect_activity(store.root / 'history.sqlite3', {200}) == ['No local activity.']


def test_usage_attribution_isolated_across_concurrent_tasks(tmp_path, monkeypatch):
    monkeypatch.setenv('ACTIVITY_TEST_KEY', 'test')
    store = Store(tmp_path, 'rowan')
    async def scenario():
        provider = Provider({'api_key_env': 'ACTIVITY_TEST_KEY', 'base_url': 'https://example.org', 'model': 'test'})
        await provider.client.aclose()
        async def respond(request):
            channel = int(json.loads(request.content)['messages'][0]['content'])
            await asyncio.sleep(0)
            return httpx.Response(200, json={'choices': [{'message': {'content': 'PRIVATE REASONING'}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': channel, 'completion_tokens': 5}})
        provider.client = httpx.AsyncClient(base_url='https://example.org', transport=httpx.MockTransport(respond))
        async def work(channel):
            for _ in range(2):
                await provider.complete([{'role': 'user', 'content': str(channel)}])
        try:
            await asyncio.gather(*(observe(store, channel, 'invoked', lambda c=channel: work(c)) for channel in (100, 200)))
        finally:
            await provider.close()
    asyncio.run(scenario())
    for channel, usage in store.db.execute('SELECT channel,usage FROM activity'):
        assert [item['prompt_tokens'] for item in json.loads(usage)] == [int(channel)] * 2
        assert 'PRIVATE REASONING' not in usage
    assert current.get() is None


def test_failure_and_legacy_jobs_show_actual_delivery_state(tmp_path):
    current_job, _ = job(tmp_path)
    store = current_job.store
    store.delivery(current_job.id, 0, 5)
    store.delivery(current_job.id, 1, kind='reaction', state='started')
    legacy = '\n'.join(inspect_activity(store.root / 'history.sqlite3', {100}))
    assert 'origin unknown' in legacy and 'sent×1' in legacy and 'delivery uncertain×1' in legacy
    async def fail():
        current.get()['job'] = current_job.id
        raise ValueError('PRIVATE FAILURE')
    with pytest.raises(ValueError):
        asyncio.run(observe(store, 100, 'invoked', fail))
    report = '\n'.join(inspect_activity(store.root / 'history.sqlite3', {100}))
    assert 'failed' in report and 'sent×1' in report and 'PRIVATE FAILURE' not in report
    assert report.count('job ' + current_job.id) == 1


def test_reader_does_not_migrate_legacy_database_or_load_credentials(tmp_path, monkeypatch, capsys):
    from bevvycord import __main__ as cli
    current_job, _ = job(tmp_path)
    store = current_job.store
    store.db.execute('DROP TABLE activity')
    store.db.commit()
    database = store.root / 'history.sqlite3'
    before = database.read_bytes()
    monkeypatch.setattr(cli, 'load_config', lambda _: {'storage_dir': str(store.root.parent),
        'character': {'id': store.root.name}, 'allowed_channel_ids': {100}})
    def forbidden(*args):
        raise AssertionError('Credentials must not be loaded')
    monkeypatch.setattr(cli, 'load_env_file', forbidden)
    monkeypatch.setattr('sys.argv', ['bevvycord', '--activity', '--job', current_job.id])
    cli.main()
    assert 'origin unknown' in capsys.readouterr().out
    assert database.read_bytes() == before


def test_cancelled_activity_and_restart_recovery(tmp_path):
    store = Store(tmp_path, 'rowan')
    async def cancelled():
        raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(observe(store, 100, 'check-in', cancelled))
    assert store.db.execute('SELECT state FROM activity').fetchone()[0] == 'cancelled'
    store.db.execute("UPDATE activity SET state='running',ended=NULL")
    store.db.commit()
    store.recover_jobs()
    state, ended = store.db.execute('SELECT state,ended FROM activity').fetchone()
    assert state == 'interrupted' and ended is not None


def test_reaction_only_and_silent_classification(tmp_path):
    current_job, _ = job(tmp_path)
    store = current_job.store
    async def work():
        current.get()['job'] = current_job.id
        store.receipt_start(current_job.id, 'finish', 'finish', '{}')
        store.receipt_finish(current_job.id, 'finish', '{"status":"finished"}')
    asyncio.run(observe(store, 100, 'check-in', work))
    assert 'silent' in '\n'.join(inspect_activity(store.root / 'history.sqlite3', {100}))
    store.delivery(current_job.id, 0, 1, kind='reaction', emoji='❤️')
    report = '\n'.join(inspect_activity(store.root / 'history.sqlite3', {100}))
    assert 'reacted×1' in report and 'silent' not in report
