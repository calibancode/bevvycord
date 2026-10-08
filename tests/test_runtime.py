import asyncio
import copy
import json
from pathlib import Path

import pytest

from bevvycord.config import tool_settings
from bevvycord.context import Message, Window
from bevvycord.jobs import Attachment, Job, clean_jobs
from bevvycord.memory import rebuild_memory
from bevvycord.runtime import Runtime
from bevvycord.storage import Store
from bevvycord.tools import Registry, arguments, builtin_registry


def call(name, args, identifier='c1'):
    return {'id': identifier, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}


def response(*calls):
    return {'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None,
            'reasoning_content': 'PRIVATE INTERNAL REASONING', 'tool_calls': list(calls)}}


FINAL = {'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'Done.'}}


class Scripted:
    def __init__(self, *replies):
        self.replies, self.requests, self.options = list(replies), [], []
    async def complete(self, messages, **kwargs):
        self.requests.append(copy.deepcopy(messages))
        self.options.append(kwargs)
        return self.replies.pop(0)


def job(tmp_path, memory=True, **limits):
    store = Store(tmp_path, 'rowan')
    window = Window([Message(1, 7, 'Bevvy', '2026-10-07', 'Remember my preference', False)])
    settings = tool_settings({'tools': {'enabled': True, **limits}})
    return Job(store, 100, 7, 1, window, settings), builtin_registry(settings, memory)


def test_replay_errors_receipts_and_ephemeral_context(tmp_path):
    current, registry = job(tmp_path)
    provider = Scripted(response(call('write_file', {'path': 'result.txt', 'content': 'hello'})),
                        response(call('write_file', {'path': 'result.txt', 'content': 'hello'})),
                        response(call('write_file', {'path': 'other.txt', 'content': 'danger'})),
                        response(call('unknown', {}, 'unknown'), call('exec', {'command': 5}, 'bad-schema')),
                        FINAL)
    inputs = [{'role': 'system', 'content': 'You are Rowan'}, {'role': 'user', 'content': 'chat'}]
    result = asyncio.run(Runtime(provider, registry, current.settings).run(inputs, current))
    assert result.answer == 'Done.'
    assert inputs[-1]['content'] == 'chat'
    assert not (current.work / 'other.txt').exists()
    assert provider.requests[1][3]['reasoning_content'] == 'PRIVATE INTERNAL REASONING'
    assert provider.requests[1][4]['tool_call_id'] == 'c1'
    assert 'reused with different arguments' in provider.requests[3][-1]['content']
    assert 'Unknown tool' in provider.requests[-1][-2]['content']
    assert 'schema' in provider.requests[-1][-1]['content']
    assert current.store.db.execute('SELECT count(*) FROM tool_receipts').fetchone()[0] == 3
    assert 'PRIVATE INTERNAL REASONING' not in current.store.archive(100)[0]


def test_memory_side_effect_survives_failure_and_rebuild(tmp_path):
    current, registry = job(tmp_path)
    provider = Scripted(response(call('remember', {'note': 'I remember Bevvy prefers short answers', 'message_ids': [1]})))
    runtime = Runtime(provider, registry, current.settings)
    with pytest.raises(IndexError):
        asyncio.run(runtime.run([{'role': 'user', 'content': 'chat'}], current))
    current.store.job_state(current.id, 'failed')
    settings = {'quiet_minutes': 999, 'cooldown_hours': 999, 'max_wait_hours': 999}
    assert current.store.due_memory_channels(settings) == ['100']
    current.store.memory_failed(100, 30)
    assert current.store.due_memory_channels(settings) == []
    current.store.clock = lambda: 10**12
    class Writer:
        async def generate(self, messages, **kwargs):
            assert 'prefers short answers' in str(messages)
            return '- Bevvy prefers short answers.'
    cfg = {'character': {'prompt': 'You are Rowan'}, 'provider': {'model': 'fake'}, 'memory': {'enabled': True}}
    asyncio.run(rebuild_memory(current.store, Writer(), cfg, 100))
    assert current.store.pending_channels() == []
    assert current.store.memory(100).startswith('- Bevvy')
    assert current.store.db.execute('SELECT applied FROM memory_notes').fetchall() == [(1,)]


def test_uncertain_receipt_not_reexecuted_and_restart(tmp_path):
    current, registry = job(tmp_path)
    args = {'path': 'unsafe.txt', 'content': 'repeat'}
    current.store.receipt_start(current.id, 'uncertain', 'write_file', json.dumps(args, sort_keys=True))
    result = asyncio.run(Runtime(None, registry, current.settings).dispatch(current, call('write_file', args, 'uncertain')))
    assert 'uncertain' in result['error']
    assert not (current.work / 'unsafe.txt').exists()
    current.store.recover_jobs()
    assert current.store.db.execute('SELECT state FROM jobs').fetchone()[0] == 'interrupted'


def test_file_boundaries_freezing_and_attachment_limits(tmp_path):
    current, registry = job(tmp_path, file_bytes=100, workspace_bytes=1000)
    for value in ('../secret', '/etc/passwd', '.', '/workspace/../secret'):
        with pytest.raises(ValueError): current.path(value)
    (current.work / 'link').symlink_to('/etc')
    with pytest.raises(ValueError): current.read_file('link/passwd')
    current.write_file('output.txt', 'original')
    receipt = current.return_file('output.txt')
    current.write_file('output.txt', 'modified')
    assert current.artifacts[0].path.read_text() == 'original'
    assert receipt['bytes'] == 8
    with pytest.raises(ValueError): current.return_file('output.txt', '../bad')
    with pytest.raises(ValueError): current.write_file('huge', 'x' * 101)
    downloaded = []
    async def download(path, maximum):
        downloaded.append(path)
        path.write_text('data')
    current.attachments = {'1:2': Attachment('1:2', '../../host-name.txt', 4, download),
                           '1:3': Attachment('1:3', 'huge', 101, download)}
    async def scenario():
        result = await current.get_attachment('1:2')
        assert result == await current.get_attachment('1:2')
        assert len(downloaded) == 1
        assert current.path(result['path']).read_text() == 'data'
        with pytest.raises(ValueError): await current.get_attachment('1:3')
        with pytest.raises(ValueError): await current.get_attachment('another-channel')
    asyncio.run(scenario())


def test_registry_plugins_errors_and_duplicate_names(tmp_path, monkeypatch):
    module = tmp_path / 'example_bevvy_plugin.py'
    module.write_text('''async def run(ctx, value):
    return {"result": value.upper()}
def register(registry):
    registry.add("example_upper", "Uppercase", {"type":"object", "properties":{"value":{"type":"string"}}, "required":["value"], "additionalProperties":False}, run)
''')
    monkeypatch.syspath_prepend(str(tmp_path))
    registry = Registry()
    registry.load_plugins(['example_bevvy_plugin'])
    assert asyncio.run(registry.call('example_upper', {'value': 'hi'}, None)) == {'result': 'HI'}
    with pytest.raises(ValueError): registry.load_plugins(['example_bevvy_plugin'])
    async def broken(ctx): raise OSError('possibly secret')
    registry.add('broken', 'fails', arguments({}), broken)
    result = asyncio.run(registry.call('broken', {}, None))
    assert result == {'error': 'Tool failed (OSError)'}


def test_caps_sources_scoped_continuation_and_retention(tmp_path):
    current, registry = job(tmp_path, max_steps=1)
    with pytest.raises(RuntimeError, match='final reply'):
        asyncio.run(Runtime(Scripted(response(call('read_file', {'path': 'missing'}))), registry, current.settings).run([{'role': 'user', 'content': 'chat'}], current))
    current.call_id = 'mem'
    with pytest.raises(ValueError): current.memory_note('remember', 'bad source', [999])
    current.write_file('survivor.txt', 'survived')
    current.store.job_state(current.id, 'failed')
    next_job = Job(current.store, 100, 7, 2, current.window, current.settings)
    restored = next_job.open_job(current.id)
    assert next_job.path(restored['path'] + '/survivor.txt').read_text() == 'survived'
    other = Job(current.store, 200, 7, 3, current.window, current.settings)
    with pytest.raises(ValueError): other.open_job(current.id)
    stranger = Job(current.store, 100, 8, 3, current.window, current.settings)
    assert stranger.open_job(current.id)['initiator_id'] == '7'
    autonomous = Job(current.store, 100, 9, 3, current.window, current.settings, initiative=True)
    assert autonomous.open_job(current.id)['requester_id'] == '7'
    assert autonomous.open_job(current.id)['character_id'] == current.store.root.name
    foreign_store = Store(tmp_path / 'foreign', 'another')
    foreign = Job(foreign_store, 100, 7, 3, current.window, current.settings)
    with pytest.raises(ValueError, match='character and channel'): foreign.open_job(current.id)
    foreign_store.db.close()
    current.store.clock = lambda: 10**12
    clean_jobs(current.store, current.settings)
    assert not current.work.exists()
    assert next_job.work.exists() # Active job never cleaned.


def test_turn_timeout_leaves_started_receipt_uncertain(tmp_path):
    current, _ = job(tmp_path, turn_seconds=1)
    registry = Registry()
    async def hang(ctx): await asyncio.Event().wait()
    registry.add('hang', 'hang', arguments({}), hang)
    runtime = Runtime(Scripted(response(call('hang', {}))), registry, current.settings)
    with pytest.raises(TimeoutError):
        asyncio.run(runtime.run([{'role': 'user', 'content': 'chat'}], current))
    assert current.store.db.execute('SELECT result FROM tool_receipts').fetchone()[0] is None


def test_call_limit_and_malformed_arguments_recover(tmp_path):
    current, registry = job(tmp_path, max_calls=2)
    malformed = call('write_file', {})
    malformed['function']['arguments'] = '{invalid'
    provider = Scripted(response(malformed),
                        response(call('write_file', {'path': 'ran', 'content': 'x'}, 'c2'),
                                 call('write_file', {'path': 'not-run', 'content': 'x'}, 'c3')), FINAL)
    result = asyncio.run(Runtime(provider, registry, current.settings).run([{'role': 'user', 'content': 'chat'}], current))
    assert result.answer == 'Done.'
    assert 'valid JSON' in provider.requests[1][-1]['content']
    assert 'call limit' in provider.requests[2][-2]['content']
    assert not (current.work / 'not-run').exists()
    assert (current.work / 'ran').exists()
    assert provider.options[-1]['tool_choice'] == 'auto'


def test_last_model_request_is_reserved_for_final_reply(tmp_path):
    current, registry = job(tmp_path, max_steps=2)
    provider = Scripted(response(call('write_file', {'path': 'done.txt', 'content': 'done'})), FINAL)
    result = asyncio.run(Runtime(provider, registry, current.settings).run([{'role': 'user', 'content': 'chat'}], current))
    assert result.answer == 'Done.'
    assert len(provider.requests) == 2
    assert 'tool_choice' not in provider.options[0]
    assert provider.options[1]['tool_choice'] == 'auto'
    assert [tool['function']['name'] for tool in provider.options[1]['tools']] == ['finish']
    assert 'Give your final reply' in provider.requests[-1][-1]['content']
    assert provider.requests[-1][-2]['tool_call_id'] == 'c1'


def test_provider_cannot_execute_tools_during_final_reply(tmp_path):
    current, registry = job(tmp_path, max_steps=1)
    provider = Scripted(response(call('write_file', {'path': 'not-run', 'content': 'x'})))
    with pytest.raises(RuntimeError, match='final reply'):
        asyncio.run(Runtime(provider, registry, current.settings).run([{'role': 'user', 'content': 'chat'}], current))
    assert not (current.work / 'not-run').exists()
    assert current.store.db.execute('SELECT count(*) FROM tool_receipts').fetchone()[0] == 0


def test_duplicate_call_batch_is_rejected_before_side_effects(tmp_path):
    current, registry = job(tmp_path)
    command = call('write_file', {'path': 'not-run', 'content': 'x'})
    provider = Scripted(response(command, command))
    with pytest.raises(RuntimeError, match='repeated tool call ID'):
        asyncio.run(Runtime(provider, registry, current.settings).run([{'role': 'user', 'content': 'chat'}], current))
    assert not (current.work / 'not-run').exists()


def test_final_reply_salvages_text_sent_with_stray_tool_calls(tmp_path):
    current, registry = job(tmp_path, max_steps=1)
    stray = response(call('write_file', {'path': 'not-run', 'content': 'x'}))
    stray['message']['content'] = 'Here is what I found.'
    result = asyncio.run(Runtime(Scripted(stray), registry, current.settings).run([{'role': 'user', 'content': 'chat'}], current))
    assert result.answer == 'Here is what I found.'
    assert not (current.work / 'not-run').exists()
