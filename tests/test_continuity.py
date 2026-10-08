import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS

from bevvycord.context import Message, Window, chunks, select_window, transcript
from bevvycord.discord_io import is_trigger, normalize, split_answer
from bevvycord.memory import rebuild_memory
from bevvycord.storage import Store


def msg(i, author=None, text=None):
    return Message(i, author if author is not None else i % 2, 'Speaker',
                   '2026-10-07T00:00:00+00:00', text or f'message {i}')


def test_chunks_count_runs_not_messages_and_keep_whole_oldest():
    history = [msg(i, i // 10) for i in range(1, 51)]
    window = select_window(history, soft=2, hard=4)
    assert len(chunks(window.messages)) == 2
    assert window.messages[0].id == 40
    assert len(window.messages) == 11


def test_frequent_participation_preserves_prefix_then_resets():
    previous = Window([msg(i) for i in range(1, 22)], last_response_id=21)
    next_window = select_window([msg(i) for i in range(1, 31)], previous)
    assert [m.id for m in next_window.messages] == list(range(1, 31))
    next_window.last_response_id = 30
    reset = select_window([msg(i) for i in range(1, 42)], next_window)
    assert [m.id for m in reset.messages] == list(range(22, 42))
    assert reset.gap_before is None


def test_absence_bridges_old_and_recent_then_retires_old():
    previous = Window([msg(i) for i in range(1, 22)], last_response_id=21)
    bridge = select_window([msg(i) for i in range(70, 111)], previous)
    assert [m.id for m in bridge.messages] == list(range(2, 22)) + list(range(91, 111))
    assert bridge.gap_before == 91
    bridge.messages.append(msg(111))
    bridge.last_response_id = 111
    growing = select_window([msg(i) for i in range(80, 113)], bridge)
    assert len(chunks(growing.messages, growing.gap_before)) == 40
    assert growing.gap_before == 91
    assert growing.messages[0].id == 4
    growing.last_response_id = 112
    retired = select_window([msg(i) for i in range(90, 133)], growing)
    assert len(chunks(retired.messages)) == 20
    assert retired.gap_before is None
    assert retired.messages[-1].id == 132


def test_continuous_gap_fits_without_note():
    previous = Window([msg(i) for i in range(1, 6)], last_response_id=5)
    result = select_window([msg(i) for i in range(1, 31)], previous)
    assert len(result.messages) == 30
    assert result.gap_before is None


def test_repeated_absences_never_accumulate_windows():
    previous = Window([msg(i) for i in range(1, 22)], last_response_id=21)
    for start in (70, 170, 270):
        result = select_window([msg(i) for i in range(start, start + 41)], previous)
        assert len(chunks(result.messages, result.gap_before)) <= 40
        assert transcript(result).count('[Context note:') == 1
        result.last_response_id = start + 40
        previous = result


def test_queued_trigger_does_not_see_future_answer():
    previous = Window([msg(1), msg(5)], last_response_id=5)
    result = select_window([msg(1), msg(3)], previous)
    assert [m.id for m in result.messages] == [1, 3]


def test_trigger_included_once_and_updates_are_used():
    previous = Window([msg(1), msg(2)], last_response_id=2)
    result = select_window([msg(1, text='edited'), msg(2), msg(3), msg(3)], previous)
    assert [m.id for m in result.messages] == [1, 2, 3]
    assert result.messages[0].text == 'edited'


def test_messages_during_generation_reappear_next_turn():
    previous = Window([msg(1), msg(2), msg(5)], last_response_id=5)
    history = [msg(i) for i in range(1, 7)]
    result = select_window(history, previous)
    assert [m.id for m in result.messages] == list(range(1, 7))
    previous = Window([msg(1), msg(20), msg(23)], gap_before=20, last_response_id=23)
    result = select_window([msg(i) for i in range(1, 25)], previous)
    assert [m.id for m in result.messages] == [1] + list(range(20, 25))
    assert result.gap_before == 20


def test_stale_completion_does_not_revert_edit_or_resurrect_delete(tmp_path):
    store = Store(tmp_path, 'rowan')
    original = Window([msg(1), msg(2)], last_response_id=2)
    store.complete(100, original, 'first')
    store.update(100, msg(1, text='corrected'))
    store.delete(100, 2)
    store.complete(100, original, 'stale snapshot')
    current = store.previous(100)
    assert [m.id for m in current.messages] == [1]
    assert current.messages[0].text == 'corrected'
    archive, _, _ = store.archive(100)
    assert 'corrected' in archive
    assert 'message:2]' not in archive


def test_archive_changes_schedule_memory_without_new_participation(tmp_path):
    store = Store(tmp_path, 'rowan')
    store.complete(100, Window([msg(1), msg(2)]), 'first')
    _, checkpoint, _ = store.archive(100)
    store.write_memory(100, '- original', checkpoint)
    store.update(100, msg(1))
    assert store.pending_channels() == []
    store.update(100, msg(1, text='correction'))
    assert store.pending_channels() == ['100']
    store.write_memory(100, '- corrected', checkpoint)
    assert store.pending_channels() == []
    store.delete(100, 2)
    assert store.pending_channels() == ['100']


def test_renames_refresh_identity_without_false_edits(tmp_path):
    from dataclasses import replace
    store = Store(tmp_path, 'rowan')
    original = msg(1)
    store.complete(100, Window([original]), 'first')
    store.write_memory(100, '- original', store.checkpoint(100))
    renamed = replace(original, name='Bevvy!', username='bevvy2')
    store.update(100, renamed)
    assert store.pending_channels() == []
    assert '<edited_messages' not in store.changes(100)[0]
    assert store.previous(100).messages == [renamed]
    edited = replace(renamed, reply_to=99)
    store.update(100, edited)
    changed = store.db.execute('SELECT changed FROM messages').fetchone()[0]
    store.update(100, replace(edited, name='Another name'))
    assert store.db.execute('SELECT changed FROM messages').fetchone()[0] == changed
    assert store.pending_channels() == ['100']
    assert '<edited_messages' in store.changes(100)[0]


def test_channel_and_character_isolation_archive_dedup_and_deletions(tmp_path):
    store = Store(tmp_path, 'rowan')
    store.complete(100, Window([msg(1), msg(2)], last_response_id=2), 'now')
    store.complete(100, Window([msg(1), msg(2), msg(3)], last_response_id=3), 'later')
    assert store.previous(200) is None
    assert Store(tmp_path, 'mira').previous(100) is None
    store.update(100, msg(1, text='corrected'))
    store.delete(100, 2)
    archive, checkpoint, _ = store.archive(100)
    assert archive.count('message:1]') == 1
    assert 'corrected' in archive
    assert 'message:2]' not in archive
    assert [m.id for m in store.previous(100).messages] == [1, 3]
    store.write_memory(100, '- useful', checkpoint)
    assert store.memory(200) == ''
    assert store.pending_channels() == []


def trigger(**changes):
    values = dict(author=NS(id=1, bot=False), webhook_id=None, guild=NS(id=5),
                  channel=NS(id=100), mentions=[NS(id=9)], reference=None)
    values.update(changes)
    return NS(**values)


def test_only_humans_in_allowed_channels_invoke():
    assert is_trigger(trigger(), 9, {100}, set())
    assert not is_trigger(trigger(author=NS(id=4, bot=True)), 9, {100}, set())
    assert not is_trigger(trigger(webhook_id=123), 9, {100}, set())
    assert not is_trigger(trigger(), 9, {200}, set())
    assert not is_trigger(trigger(), 9, set(), set())
    assert not is_trigger(trigger(), 9, {100}, {2})
    assert not is_trigger(trigger(guild=None), 9, {100}, set())
    assert is_trigger(trigger(mentions=[], reference=NS(resolved=NS(author=NS(id=9)))), 9, {100}, set())


def test_split_answers_preserves_all_text():
    for text in ('x' * 6000, ('A sentence.\n' * 900), 'a ' * 2500):
        pieces = split_answer(text)
        assert ''.join(pieces) == text
        assert all(0 < len(piece) <= 2000 for piece in pieces)


def test_other_llm_embed_and_component_answers_are_read():
    message = NS(id=1, content='', author=NS(id=2, display_name='Mira', bot=True),
                 created_at=datetime.now(timezone.utc), reference=None, webhook_id=None,
                 components=[NS(children=[NS(content='component answer')])],
                 embeds=[NS(type='rich', title=None, description='embed answer', fields=[])],
                 attachments=[], stickers=[])
    assert normalize(message).text == 'component answer\nembed answer'


def test_memory_refresh_rewrites_from_archive_and_current_memory(tmp_path):
    store = Store(tmp_path, 'rowan')
    store.complete(100, Window([msg(1, text='original fact'), msg(2, author=9, text='my reply')], last_response_id=2), 'now')
    store.memory_path(100).write_text('OLD SUMMARY')
    class FakeProvider:
        async def generate(self, messages, **kwargs):
            assert messages[0]['content'].startswith('You are Rowan')
            assert 'Your Discord speaker ID is bot:9' in messages[0]['content']
            assert 'Write in character, in your own voice' in messages[0]['content']
            assert 'Do not invent' not in messages[0]['content']
            payload = '\n'.join(m['content'] for m in messages)
            assert 'original fact' in payload
            assert '<current_memory>\nOLD SUMMARY\n</current_memory>' in payload
            return '- Fresh recollection'
    config = {'character': {'prompt': 'You are Rowan'}, 'provider': {'model': 'test'}, 'memory': {'enabled': True}}
    assert asyncio.run(rebuild_memory(store, FakeProvider(), config, 100))
    assert store.memory(100) == '- Fresh recollection\n'
    assert store.memory_path(100).with_name('MEMORY.previous.md').read_text() == 'OLD SUMMARY'


def test_oversized_archive_leaves_memory_untouched(tmp_path):
    store = Store(tmp_path, 'rowan')
    store.complete(100, Window([msg(1)]), 'now')
    store.memory_path(100).write_text('- existing')
    config = {'character': {'prompt': 'Rowan'}, 'provider': {'model': 'test'},
              'memory': {'enabled': True, 'max_archive_chars': 1}}
    import pytest
    with pytest.raises(ValueError, match='archive exceeds'):
        asyncio.run(rebuild_memory(store, None, config, 100))
    assert store.memory(100) == '- existing'


def test_gateway_and_rest_copies_normalize_to_the_same_speaker_name():
    # Gateway messages carry a Member (nickname); REST fetches carry a User.
    common = dict(id=1, content='hi', created_at=datetime.now(timezone.utc), reference=None,
                  webhook_id=None, components=[], embeds=[], attachments=[], stickers=[])
    gateway = NS(**common, author=NS(id=2, display_name='Server Nick', global_name='Bevvy', name='bevvy', bot=False))
    rest = NS(**common, author=NS(id=2, display_name='Bevvy', global_name='Bevvy', name='bevvy', bot=False))
    assert normalize(gateway) == normalize(rest)
    assert normalize(gateway).name == 'Bevvy'
    assert normalize(gateway).username == 'bevvy'


def test_transcript_identity_survives_name_changes_and_old_windows():
    from dataclasses import replace
    from bevvycord.context import transcript
    original = replace(msg(1), name='Bevvy', username='bevvy')
    renamed = replace(original, id=2, name='New Name', username='new_username')
    assert original.speaker == renamed.speaker
    assert 'username:"bevvy"; user:' in transcript(Window([original]))
    old = Window([replace(original, username=None)]).dumps()
    assert Window.loads(old).messages[0].username is None
    import json
    legacy = json.loads(old)
    del legacy['messages'][0]['username']
    assert Window.loads(json.dumps(legacy)).messages[0].username is None


def test_threads_never_trigger_even_when_listed():
    import discord
    from unittest.mock import MagicMock
    from bevvycord.discord_io import is_trigger
    thread = MagicMock(spec=discord.Thread)
    thread.id = 5
    message = NS(author=NS(id=1, bot=False), webhook_id=None, guild=object(), channel=thread,
                 mentions=[NS(id=9)], reference=None)
    assert not is_trigger(message, 9, {5}, set())
    message.channel = NS(id=5)
    assert is_trigger(message, 9, {5}, set())
