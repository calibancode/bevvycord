import asyncio
import json
import os
from pathlib import Path
import threading

import pytest
import yaml

from bevvycord.config import LIBRARY_DEFAULTS, load_config
from bevvycord.jobs import Job, clean_jobs
from bevvycord.library import Library
from bevvycord.prompts import reply_messages
from bevvycord.runtime import Runtime
from bevvycord.storage import Store
from bevvycord.tools import builtin_registry
from test_runtime import job, Scripted, response, call


def later(original, channel=100, actor=9, **kwargs):
    return Job(original.store, channel, actor, 2, original.window, original.settings,
               initiative=True, **kwargs)


def test_save_expiry_restart_and_passive_reuse_without_reminder(tmp_path):
    async def scenario():
        first, registry = job(tmp_path)
        messages = reply_messages('Be yourself.', 9, first.window)
        provider = Scripted(response(call('write_file', {'path': 'projects/poem.txt', 'content': 'My unfinished poem'})),
                            response(call('library_save', {'path': 'projects/poem.txt', 'name': 'poems/draft.txt'}, 'save')),
                            response(call('finish', {}, 'done')))
        await Runtime(provider, registry, first.settings).run(messages, first)
        assert '<library_files>' not in str(provider.requests[0])
        record = first.store.db.execute("SELECT result FROM tool_receipts WHERE name='library_save'").fetchone()
        assert json.loads(record[0])['origin']['job_id'] == first.id
        first.store.job_state(first.id, 'complete')
        first.store.clock = lambda: 10**12
        clean_jobs(first.store, first.settings)
        assert not first.work.exists()
        first.store.db.close()
        store = Store(tmp_path, 'rowan')
        next_job = Job(store, 100, 9, 2, first.window, first.settings, initiative=True)
        # No instruction to inspect or retrieve the library in the conversation.
        second = Scripted(response(call('library_get', {'name': 'poems/draft.txt', 'path': 'draft.txt'})),
                          response(call('read_file', {'path': 'draft.txt'}, 'read')),
                          response(call('finish', {}, 'done')))
        await Runtime(second, registry, next_job.settings).run(messages, next_job)
        assert second.requests[0][:-1] == messages
        context = second.requests[0][-1]['content']
        assert '<library_files>' in context and 'poems/draft.txt' in context
        assert 'My unfinished poem' not in context  # Metadata only.
        assert next_job.path('draft.txt').read_text() == 'My unfinished poem'
        assert store.db.execute('SELECT COUNT(*) FROM memory_notes').fetchone()[0] == 0
        assert not (store.root / '100' / 'MEMORY.md').exists()
        store.db.close()
    asyncio.run(scenario())


def test_channel_default_personal_opt_in_and_character_isolation(tmp_path):
    async def scenario():
        current, _ = job(tmp_path)
        current.write_file('private.txt', 'channel attachment')
        await current.library.save(current, 'private.txt')
        await current.library.save(current, 'private.txt', 'personal.txt', scope='personal')
        other_channel = later(current, channel=200)
        listing = other_channel.library.list(other_channel)
        assert [f['name'] for f in listing['files']] == ['personal.txt']
        assert 'private.txt' not in other_channel.library_context()
        for action in ('get', 'delete'):
            with pytest.raises(ValueError, match='No saved file'):
                if action == 'get':
                    await current.library.get(other_channel, 'private.txt', 'leak.txt')
                else:
                    await current.library.delete(other_channel, 'private.txt')
        result = await current.library.get(other_channel, 'personal.txt', 'mine.txt', scope='personal')
        assert result['origin']['channel_id'] == '100'
        store = Store(tmp_path, 'scooter')
        sibling = Job(store, 100, 7, 1, current.window, current.settings)
        assert sibling.library.list(sibling)['files'] == []
        with pytest.raises(ValueError, match='No saved file'):
            await sibling.library.get(sibling, 'personal.txt', 'leak.txt', scope='personal')
        # Namespace collisions between personal and channel are explicit, never fallback.
        await current.library.save(current, 'private.txt', 'personal.txt')
        assert len(current.library.list(current)['files']) == 3
        await current.library.delete(other_channel, 'personal.txt', scope='personal')
        assert other_channel.path('mine.txt').read_text() == 'channel attachment'
        assert len(current.library.list(current)['files']) == 2
        store.db.close()
        current.store.db.close()
    asyncio.run(scenario())


def test_quota_replace_and_operation_bounds_preserve_existing_content(tmp_path):
    async def scenario():
        current, _ = job(tmp_path)
        current.library.settings = {**LIBRARY_DEFAULTS, 'max_bytes': 5, 'max_files': 1, 'file_bytes': 5}
        current.write_file('a.txt', '12345')
        current.write_file('b.txt', '123456')
        await current.library.save(current, 'a.txt', 'saved.txt')
        with pytest.raises(ValueError, match='already exists'):
            await current.library.save(current, 'b.txt', 'saved.txt')
        with pytest.raises(ValueError, match='limit'):
            await current.library.save(current, 'b.txt', 'saved.txt', overwrite=True)
        with pytest.raises(ValueError, match='count limit'):
            await current.library.save(current, 'a.txt', 'another.txt')
        await current.library.get(current, 'saved.txt', 'old.txt')
        assert current.path('old.txt').read_text() == '12345'
        current.write_file('small.txt', 'ok')
        await current.library.save(current, 'small.txt', 'saved.txt', overwrite=True)
        assert len(list(current.library.root.iterdir())) == 1
        await current.library.get(current, 'saved.txt', 'new.txt')
        assert current.path('new.txt').read_text() == 'ok'
        with pytest.raises(ValueError, match='destination already exists'):
            await current.library.get(current, 'saved.txt', 'new.txt')
        current.library.settings['file_bytes'] = 1
        with pytest.raises(ValueError, match='operation limits'):
            await current.library.get(current, 'saved.txt', 'too-big.txt')
        assert not current.path('too-big.txt').exists()
        current.store.db.close()
    asyncio.run(scenario())


def test_total_quota_and_concurrent_saves_are_serialized(tmp_path):
    async def scenario():
        current, _ = job(tmp_path)
        current.library.settings = {**LIBRARY_DEFAULTS, 'max_bytes': 5}
        current.write_file('a.txt', '123')
        results = await asyncio.gather(current.library.save(current, 'a.txt', 'one'),
                                       current.library.save(current, 'a.txt', 'two'), return_exceptions=True)
        assert sum(isinstance(result, ValueError) for result in results) == 1
        assert current.store.db.execute('SELECT SUM(bytes) FROM library_files').fetchone()[0] == 3
        current.store.db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('name', ['../escape', '/absolute', '.', 'a/../b', 'a//b', 'line\nbreak', 'x' * 161])
def test_unsafe_names_rejected(tmp_path, name):
    current, _ = job(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(current.library.save(current, 'file', name))
    current.store.db.close()


def test_unsafe_sources_and_destinations_and_workspace_limits(tmp_path):
    async def scenario():
        current, _ = job(tmp_path)
        current.write_file('safe.txt', 'safe')
        current.path('link').symlink_to(current.path('safe.txt'))
        with pytest.raises(ValueError, match='Symlinks'):
            await current.library.save(current, 'link')
        os.mkfifo(current.path('pipe'))
        with pytest.raises(ValueError, match='regular file'):
            await current.library.save(current, 'pipe')
        await current.library.save(current, 'safe.txt')
        with pytest.raises(ValueError):
            await current.library.get(current, 'safe.txt', '../outside')
        row = current.library.row(current, 'safe.txt', 'channel')
        blob = current.library.blob_path(row[2])
        blob.unlink()
        blob.symlink_to(current.path('safe.txt'))
        with pytest.raises(ValueError, match='unavailable or unsafe'):
            await current.library.get(current, 'safe.txt', 'unsafe-copy')
        blob.unlink()
        blob.write_text('safe')
        current.settings['workspace_files'] = 3
        with pytest.raises(ValueError, match='storage limit'):
            await current.library.get(current, 'safe.txt', 'over-count')
        assert not current.path('over-count').exists()
        current.store.db.close()
    asyncio.run(scenario())


def test_bounded_discovery_pagination_literal_prefix_and_disabled_context(tmp_path):
    async def scenario():
        current, _ = job(tmp_path)
        current.write_file('a.txt', 'data')
        clock = [1]
        current.store.clock = lambda: clock[0]
        for i in range(8):
            clock[0] += 1
            await current.library.save(current, 'a.txt', f'project%/{i}.txt')
        first = current.library.list(current, prefix='project%/', limit=3)
        assert first['total'] == 8 and first['next_offset'] == 3
        assert [f['name'] for f in first['files']] == [f'project%/{i}.txt' for i in (7, 6, 5)]
        second = current.library.list(current, prefix='project%/', limit=3, offset=3)
        assert set(f['name'] for f in first['files']).isdisjoint(f['name'] for f in second['files'])
        context = current.library_context()
        assert '7.txt' in context and '2.txt' not in context
        assert 'next_offset' in context
        assert set(json.loads(context.split('\n')[3])['files'][0]) == {'name', 'scope', 'bytes', 'modified'}
        assert current.library.list(current, prefix='project_/')['total'] == 0
        disabled = later(current, library_settings={'enabled': False})
        assert disabled.library_context() == ''
        assert 'library_save' not in disabled.environment()
        assert 'library_save' not in builtin_registry(current.settings, False, False).tools
        settings = {**current.settings, 'sandbox_enabled': False}
        assert 'library_save' not in builtin_registry(settings, False).tools
        current.store.db.close()
    asyncio.run(scenario())


def test_cancellation_settles_file_and_metadata_then_releases_lock(tmp_path, monkeypatch):
    async def scenario():
        current, _ = job(tmp_path)
        current.write_file('a.txt', 'durable')
        entered, release = threading.Event(), threading.Event()
        original = current.open_read
        def held(path):
            entered.set()
            release.wait(3)
            return original(path)
        monkeypatch.setattr(current, 'open_read', held)
        task = asyncio.create_task(current.library.save(current, 'a.txt'))
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        row = current.library.row(current, 'a.txt', 'channel')
        assert current.library.blob_path(row[2]).read_text() == 'durable'
        await current.library.delete(current, 'a.txt')
        assert not current.library.lock.locked()
        current.store.db.close()
    asyncio.run(scenario())


def test_copy_failure_leaves_no_metadata_or_partial_blob_and_recovers_orphan(tmp_path, monkeypatch):
    async def scenario():
        current, _ = job(tmp_path)
        current.write_file('a.txt', 'data')
        orphan = current.library.directory() / ('a' * 32)
        orphan.write_text('orphaned')
        original = os.fsync
        def fail(fd): raise OSError('simulated disk failure')
        monkeypatch.setattr(os, 'fsync', fail)
        with pytest.raises(OSError):
            await current.library.save(current, 'a.txt')
        assert not orphan.exists()
        assert current.library.list(current)['total'] == 0
        assert list(current.library.root.iterdir()) == []
        monkeypatch.setattr(os, 'fsync', original)
        await current.library.save(current, 'a.txt')
        current.store.db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('key,value', [('enabled', 'false'), ('max_bytes', 0), ('max_files', True),
                                      ('file_bytes', -1), ('storage_dir', ''), ('tags', True)])
def test_library_config_rejects_bad_limits(tmp_path, key, value):
    cfg = yaml.safe_load(Path('config.example.yaml').read_text())
    cfg['library'][key] = value
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError): load_config(path)


def test_configurable_storage_has_separate_character_namespaces(tmp_path):
    current, _ = job(tmp_path / 'jobs')
    cfg = yaml.safe_load(Path('config.example.yaml').read_text())
    cfg['library']['storage_dir'] = 'durable'
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(cfg))
    loaded = load_config(path)
    library = Library(current.store, {**LIBRARY_DEFAULTS, **loaded['library']})
    assert library.root == tmp_path / 'durable' / 'rowan'
    current.store.db.close()


def test_real_bot_prompt_and_activity_include_durable_work(tmp_path):
    from test_initiative import setup, human
    async def scenario():
        provider = Scripted(response(call('write_file', {'path': 'draft.txt', 'content': 'unfinished story'})),
                            response(call('library_save', {'path': 'draft.txt'}, 'save')),
                            response(call('finish', {}, 'done')),
                            response(call('library_get', {'name': 'draft.txt', 'path': 'draft.txt'}, 'get')),
                            response(call('finish', {'text': 'Still thinking about that story.'}, 'done-again')))
        bot, store, channels = setup(tmp_path, provider, [10000])
        try:
            await bot.on_message(human(channels[100], '<@9> I have an idea for a story.', ping=True))
            await bot.on_message(human(channels[100], '<@9> How are you doing?', ping=True))
            assert 'draft.txt' in provider.requests[3][-1]['content']
            assert '<library_files>' in provider.requests[3][-1]['content']
            assert 'library_save' in {t['function']['name'] for t in provider.options[3]['tools']}
            rows = store.db.execute('SELECT state FROM activity').fetchall()
            assert rows == [('complete',), ('complete',)]
            names = [r[0] for r in store.db.execute('SELECT name FROM tool_receipts')]
            assert 'library_save' in names and 'library_get' in names
            assert store.db.execute('SELECT COUNT(*) FROM memory_notes').fetchone()[0] == 0
        finally:
            await bot.close()
            store.db.close()
    asyncio.run(scenario())


def test_nested_retrieval_cannot_exceed_workspace_entry_limit(tmp_path):
    async def scenario():
        current, _ = job(tmp_path)
        current.write_file('file.txt', 'file')
        await current.library.save(current, 'file.txt')
        current.settings['workspace_files'] = 2
        with pytest.raises(ValueError, match='storage limit'):
            await current.library.get(current, 'file.txt', 'nested/again/file.txt')
        assert not current.path('nested').exists()
        current.store.db.close()
    asyncio.run(scenario())


def test_library_cannot_be_configured_under_temporary_jobs(tmp_path):
    current, _ = job(tmp_path)
    with pytest.raises(ValueError, match='temporary jobs'):
        Library(current.store, {**LIBRARY_DEFAULTS, 'storage_dir': str(current.store.root / 'jobs')})
    current.store.db.close()
