import asyncio
import sqlite3

from bevvycord.context import Message, Window
from bevvycord.memory import rebuild_memory, update_memory
from bevvycord.storage import Store

CONFIG = {'character': {'prompt': 'You are Rowan'}, 'provider': {'model': 'fake'}, 'memory': {'enabled': True}}


def msg(number, text, author=7):
    return Message(number, author, 'Bevvy' if author == 7 else 'Rowan', '2026-10-07T00:00:00+00:00', text, author == 9)


def participate(store, *messages):
    store.complete(100, Window(list(messages), last_response_id=messages[-1].id), '2026-10-07T00:00:00+00:00')


class Editor:
    """Scripted document writer; records exactly the input sent to the model."""
    def __init__(self, rewrite='- Rewritten'):
        self.rewrite, self.inputs, self.systems = rewrite, [], []
    async def generate(self, messages, **kwargs):
        self.inputs.append(messages[1]['content'])
        self.systems.append(messages[0]['content'])
        return self.rewrite


def test_remember_is_applied_once_by_an_edit(tmp_path):
    store = Store(tmp_path, 'rowan')
    participate(store, msg(1, 'remember that I like eating shoes'), msg(2, 'Noted.', 9))
    store.write_memory(100, '- Bevvy is new here\n', 0)
    store.add_note('remember', 'j', 'c', 100, 7, 'Bevvy likes eating shoes', {1})
    writer = Editor('- Bevvy is new here\n- Bevvy likes eating shoes\n')
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
    store.write_memory(100, '- Bevvy lives at 12 Shoe Lane\n', store.checkpoint(100))
    participate(store, msg(3, 'please forget my address'), msg(4, 'Done.', 9))
    store.add_note('forget', 'j', 'c', 100, 7, "Bevvy's home address", {3})
    asyncio.run(update_memory(store, Editor('# Memory'), CONFIG, 100))
    assert store.memory(100) == '# Memory\n'
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
    asyncio.run(update_memory(store, Editor('- Bevvy has a dog'), CONFIG, 100))
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
        async def generate(self, messages, **kwargs):
            now[0] += 10
            store.delete(100, 1)
            return await super().generate(messages, **kwargs)
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


def test_document_update_consolidates_existing_memory(tmp_path):
    store = Store(tmp_path, 'rowan')
    participate(store, msg(1, 'Aaron is my brother'), msg(2, 'Right.', 9))
    store.write_memory(100, '- Aaron is my brother\n- My brother is Aaron\n', store.checkpoint(100))
    participate(store, msg(3, 'Aaron likes cars'), msg(4, 'Nice.', 9))
    writer = Editor('- Aaron is my brother and likes cars')
    assert asyncio.run(update_memory(store, writer, CONFIG, 100))
    assert store.memory(100) == '- Aaron is my brother and likes cars\n'
    assert len(writer.inputs) == 1
    assert '<new_conversation>' in writer.inputs[0]
    assert '<participation_archive>' not in writer.inputs[0]


def test_old_revision_rebuilds_without_new_conversation_after_restart(tmp_path):
    store = Store(tmp_path, 'rowan')
    participate(store, msg(1, 'I like shoes'), msg(2, 'Hi.', 9))
    store.write_memory(100, '- Old identity', store.checkpoint(100))
    store.db.execute('UPDATE memory_runs SET revision=0')
    store.db.commit()
    store.db.close()
    store = Store(tmp_path, 'rowan')
    assert store.due_memory_channels({}) == ['100']
    writer = Editor('- Bevvy (user:7) likes shoes')
    assert asyncio.run(update_memory(store, writer, CONFIG, 100))
    assert '<participation_archive>' in writer.inputs[0]
    assert 'Your Discord speaker ID is bot:9.' in writer.systems[0]
    assert not store.memory_needs_rebuild(100)
    assert store.due_memory_channels({}) == []
    store.db.close()
    store = Store(tmp_path, 'rowan')
    assert store.due_memory_channels({}) == []


def test_invalid_document_retains_memory_and_checkpoint(tmp_path):
    import pytest
    store = Store(tmp_path, 'rowan')
    participate(store, msg(1, 'Hi'), msg(2, 'Hi.', 9))
    store.write_memory(100, '- Existing', store.checkpoint(100))
    participate(store, msg(3, 'New detail'), msg(4, 'Noted.', 9))
    store.request_memory_rebuild(100)
    for document in ('', '```markdown\n- Memory\n```', 'x' * 10000):
        cfg = {**CONFIG, 'memory': {'enabled': True, 'max_archive_chars': 4000}}
        with pytest.raises(ValueError):
            asyncio.run(update_memory(store, Editor(document), cfg, 100))
        assert store.memory(100) == '- Existing\n'
        assert store.changes(100)[0]
        assert store.memory_needs_rebuild(100)


def test_rebuild_requested_during_write_remains_pending(tmp_path):
    now = [1000.0]
    store = Store(tmp_path, 'rowan', clock=lambda: now[0])
    participate(store, msg(1, 'Hi'), msg(2, 'Hi.', 9))
    store.write_memory(100, '- Existing', store.checkpoint(100))
    store.request_memory_rebuild(100)
    class Racing(Editor):
        async def generate(self, messages, **kwargs):
            now[0] += 1
            store.request_memory_rebuild(100)
            return await super().generate(messages, **kwargs)
    asyncio.run(update_memory(store, Racing(), CONFIG, 100))
    assert store.memory_needs_rebuild(100)
    assert store.due_memory_channels({}) == ['100']


def test_full_rebuild_receives_pending_deletions(tmp_path):
    store = Store(tmp_path, 'rowan')
    participate(store, msg(1, 'My private address'), msg(2, 'Noted.', 9))
    store.write_memory(100, '- My private address', store.checkpoint(100))
    store.delete(100, 1)
    store.request_memory_rebuild(100)
    writer = Editor('# Memory')
    asyncio.run(update_memory(store, writer, CONFIG, 100))
    assert '<deleted_messages' in writer.inputs[0]
    assert 'My private address' in writer.inputs[0]
    assert store.db.execute('SELECT body FROM messages WHERE id=1').fetchone()[0] == '{}'
    assert store.memory(100) == '# Memory\n'
