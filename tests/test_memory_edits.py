import asyncio
import json
import sqlite3

from bevvycord.context import Message, Window
from bevvycord.memory import apply_edit, rebuild_memory, update_memory
from bevvycord.storage import Store

CONFIG = {'character': {'prompt': 'You are Rowan'}, 'provider': {'model': 'fake'}, 'memory': {'enabled': True}}


def msg(number, text, author=7):
    return Message(number, author, 'Bevvy' if author == 7 else 'Rowan', '2026-10-07T00:00:00+00:00', text, author == 9)


def participate(store, *messages):
    store.complete(100, Window(list(messages), last_response_id=messages[-1].id), '2026-10-07T00:00:00+00:00')


def call(name, **args):
    return {'id': name, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}


class Editor:
    """Scripted writer: applies the given tool calls, then finishes."""
    def __init__(self, *calls, rewrite='- Rewritten'):
        self.calls, self.rewrite, self.inputs = list(calls), rewrite, []
    async def complete(self, messages, **kwargs):
        self.inputs.append(messages[1]['content'])
        if self.calls and messages[-1]['role'] != 'tool':
            return {'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None, 'tool_calls': self.calls}}
        return {'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'Done.'}}
    async def generate(self, messages, **kwargs):
        self.inputs.append(messages[1]['content'])
        return self.rewrite


def test_remember_is_applied_once_by_an_edit(tmp_path):
    store = Store(tmp_path, 'rowan')
    participate(store, msg(1, 'remember that I like eating shoes'), msg(2, 'Noted.', 9))
    store.memory_path(100).write_text('- Bevvy is new here\n')
    store.add_note('remember', 'j', 'c', 100, 7, 'Bevvy likes eating shoes', {1})
    writer = Editor(call('append', text='- Bevvy likes eating shoes'))
    assert asyncio.run(update_memory(store, writer, CONFIG, 100))
    assert 'remember requested by user:7] "Bevvy likes eating shoes"' in writer.inputs[0]
    assert store.memory(100) == '- Bevvy is new here\n- Bevvy likes eating shoes\n'
    assert not store.has_pending_notes(100)
    # Nothing new since: no writer call, nothing pending.
    assert not asyncio.run(update_memory(store, None, CONFIG, 100))
    assert store.pending_channels() == []


def test_forget_notes_stand_in_every_full_refresh(tmp_path):
    store = Store(tmp_path, 'rowan')
    participate(store, msg(1, 'I live at 12 Shoe Lane'), msg(2, 'Nice.', 9))
    store.memory_path(100).write_text('- Bevvy lives at 12 Shoe Lane\n')
    participate(store, msg(3, 'please forget my address'), msg(4, 'Done.', 9))
    store.add_note('forget', 'j', 'c', 100, 7, "Bevvy's home address", {3})
    asyncio.run(update_memory(store, Editor(call('replace', old='- Bevvy lives at 12 Shoe Lane\n', new='')), CONFIG, 100))
    assert store.memory(100) == '\n'
    writer = Editor(rewrite='- Bevvy is around')
    asyncio.run(rebuild_memory(store, writer, CONFIG, 100))
    assert 'forget requested by user:7] "Bevvy\'s home address"' in writer.inputs[0]


def test_edits_and_deletions_reach_the_next_update_then_deleted_text_is_purged(tmp_path):
    store = Store(tmp_path, 'rowan')
    participate(store, msg(1, 'I have a cat'), msg(2, 'I like tea'), msg(3, 'Cute.', 9))
    store.write_memory(100, '- Bevvy has a cat\n- Bevvy likes tea', store.checkpoint(100))
    store.update(100, msg(1, 'I have a dog'))
    store.delete(100, 2)
    changes, _, _ = store.changes(100)
    assert '<edited_messages' in changes and 'I have a dog' in changes
    assert '<deleted_messages' in changes and 'I like tea' in changes
    asyncio.run(update_memory(store, Editor(call('replace', old='- Bevvy likes tea', new='')), CONFIG, 100))
    assert store.db.execute('SELECT body FROM messages WHERE id=2').fetchone()[0] == '{}'
    assert store.changes(100)[0] == ''


def test_deletion_during_a_writer_call_is_kept_for_the_next_update(tmp_path):
    store = Store(tmp_path, 'rowan')
    now = [1000.0]
    store.clock = lambda: now[0]
    participate(store, msg(1, 'I like tea'), msg(2, 'Ok.', 9))
    store.write_memory(100, '- Bevvy likes tea', store.checkpoint(100))
    participate(store, msg(3, 'hello'), msg(4, 'Hi.', 9))
    class Racing(Editor):
        async def complete(self, messages, **kwargs):
            now[0] += 10
            store.delete(100, 1)
            return await super().complete(messages, **kwargs)
    asyncio.run(update_memory(store, Racing(), CONFIG, 100))
    assert 'I like tea' in store.changes(100)[0]


def test_remember_is_withdrawn_when_its_source_is_deleted(tmp_path):
    store = Store(tmp_path, 'rowan')
    participate(store, msg(1, 'remember I like shoes'), msg(2, 'Ok.', 9))
    store.add_note('remember', 'j', 'c', 100, 7, 'Bevvy likes shoes', {1})
    store.add_note('forget', 'j', 'd', 100, 7, 'Bevvy shoe talk', {1})
    store.delete(100, 1)
    assert [n[1] for n in store.notes(100)] == ['forget']


def test_without_memory_deleted_text_is_dropped_immediately(tmp_path):
    store = Store(tmp_path, 'rowan', retain_deleted=False)
    participate(store, msg(1, 'secret'), msg(2, 'Ok.', 9))
    store.delete(100, 1)
    assert store.db.execute('SELECT body FROM messages WHERE id=1').fetchone()[0] == '{}'


def test_edit_tool_requires_a_unique_match():
    draft = '- a\n- a\n'
    assert apply_edit(draft, 'replace', {'old': '- a', 'new': '- b'}) == (draft, {'error': '"old" occurs 2 times; it must match exactly once'})
    assert apply_edit('- a\n', 'append', {'text': '- b'})[0] == '- a\n- b\n'
    assert apply_edit(draft, 'delete_everything', {})[1] == {'error': 'Malformed edit'}


def test_legacy_memory_requests_become_notes(tmp_path):
    (tmp_path / 'rowan').mkdir()
    db = sqlite3.connect(tmp_path / 'rowan' / 'history.sqlite3')
    db.execute('CREATE TABLE memory_requests (job TEXT, call_id TEXT, channel TEXT, actor TEXT, created REAL, note TEXT, source TEXT)')
    db.execute("INSERT INTO memory_requests VALUES ('j','c','100','7',1.0,'old note','LONG TRANSCRIPT COPY')")
    db.commit()
    db.close()
    store = Store(tmp_path, 'rowan')
    assert [n[1:] for n in store.notes(100)] == [('remember', '7', 1.0, 'old note')]
    assert not store.db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_requests'").fetchone()
