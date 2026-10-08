import asyncio
import copy
import json
from types import SimpleNamespace as NS

import discord
import pytest

from bevvycord.bot import CharacterBot
from bevvycord.context import Message as Entry, Window, select_window
from bevvycord.discord_io import reaction_snapshot
from bevvycord.runtime import Runtime
from bevvycord.storage import Store
from test_agent_flow import Channel, Message, config
from test_runtime import Scripted, call, response, FINAL, job


def setup(tmp_path, provider, clock, channels=None):
    cfg = config()
    cfg['memory']['enabled'] = False
    cfg['initiative'] = {'enabled': True, 'interval_minutes': 30, 'channel_ids': {100}}
    store = Store(tmp_path, 'rowan', clock=lambda: clock[0])
    bot = CharacterBot(cfg, store, provider)
    bot._connection.user = NS(id=9, name='faust', display_name='Faust', bot=True)
    channels = channels or {100: Channel(), 200: Channel(200)}
    bot.get_channel = channels.get
    bot.get_user = lambda actor: NS(id=actor, name=f'user-{actor}', bot=actor in (9, 10))
    return bot, store, channels


def human(channel, text='new conversation', actor=7, ping=False):
    message = Message(channel, text, actor)
    message.mentions = [NS(id=9)] if ping else []
    return message


class Reaction:
    def __init__(self, emoji, normal=(), burst=()):
        self.emoji, self.normal, self.burst = emoji, list(normal), list(burst)
        self.normal_count, self.burst_count = len(normal), len(burst)
        self.count = self.normal_count + self.burst_count
    async def users(self, limit=None, type=None):
        items = self.burst if type == discord.ReactionType.burst else self.normal
        for user in sorted(items, key=lambda u: u.id)[:limit]:
            yield user


def test_optional_checkin_silence_and_no_repeated_or_bot_wakeups(tmp_path):
    async def scenario():
        clock = [10000]
        provider = Scripted(response(call('finish', {})))
        bot, store, channels = setup(tmp_path, provider, clock)
        main, testing = channels[100], channels[200]
        try:
            message = human(main)
            await bot.on_message(message)
            await bot.on_message(human(testing, 'testing only'))
            clock[0] += 1799
            await bot.initiative_tick()
            assert not provider.requests
            clock[0] += 1
            await bot.initiative_tick()
            assert len(provider.requests) == 1
            assert 'catching up on the channel' in provider.requests[0][-1]['content']
            assert 'testing only' not in str(provider.requests[0])
            assert not main.sent_calls and not testing.sent_calls
            assert store.previous(100).last_seen_id == message.id
            assert store.previous(100).last_response_id is None
            assert store.db.execute('SELECT state FROM jobs').fetchall() == [('complete',)]
            assert store.attention(100)[:2] == (1, 1)
            for _ in range(2):
                await bot.on_message(human(main, 'another bot speaks', actor=9))
                clock[0] += 1800
                await bot.initiative_tick()
            assert len(provider.requests) == 1
        finally:
            await bot.close()
            store.db.close()
    asyncio.run(scenario())


def test_checkin_plain_message_or_explicit_reply_target(tmp_path):
    async def scenario():
        clock = [10000]
        provider = Scripted(response(call('finish', {'text': 'Joining in.'})),
                            response(call('finish', {'text': 'That was fun.', 'reply_to': 1}, 'again')))
        bot, store, channels = setup(tmp_path, provider, clock)
        channel = channels[100]
        try:
            await bot.on_message(human(channel))
            clock[0] += 1800
            await bot.initiative_tick()
            assert channel.sent_calls[0]['reference'] is None
            assert channel.sent_calls[0]['content'] == 'Joining in.'
            await bot.on_message(human(channel, 'more conversation'))
            clock[0] += 1800
            await bot.initiative_tick()
            assert channel.sent_calls[1]['reference'] == 1
            assert channel.sent_calls[1]['content'] == 'That was fun.'
        finally:
            await bot.close()
            store.db.close()
    asyncio.run(scenario())


def test_reaction_attribution_wakes_only_for_humans_and_can_react_without_text(tmp_path):
    async def scenario():
        clock = [10000]
        provider = Scripted(FINAL, response(call('finish', {'reactions': [{'message_id': 1, 'emoji': '❤️'}]}, 'react')))
        bot, store, channels = setup(tmp_path, provider, clock)
        channel = channels[100]
        try:
            trigger = human(channel, '<@9> hello', ping=True)
            await bot.on_message(trigger)
            outgoing = channel.messages[2]
            bevvy = NS(id=7, name='bevvy', display_name='Bevvy', bot=False)
            outgoing.reactions = [Reaction('❤️', [bevvy])]
            await bot.on_raw_reaction_add(NS(channel_id=100, message_id=2, user_id=7, member=bevvy))
            assert store.attention(100)[:2] == (2, 1)
            clock[0] += 1800
            await bot.initiative_tick()
            assert trigger.added_reactions == ['❤️']
            assert len(channel.sent_calls) == 1 # Only the original human-invoked answer.
            assert '<reactions>' in provider.requests[-1][-2]['content']
            assert '"bevvy" (user:7)' in provider.requests[-1][-2]['content']
            assert provider.requests[-1][:-2][:len(provider.requests[0]) - 1] == provider.requests[0][:-1]
            row = store.db.execute("SELECT state,message_id,kind,emoji FROM deliveries WHERE kind='reaction'").fetchone()
            assert row == ('sent', 1, 'reaction', '❤️')
            assert '(bot:9)' in store.reactions(100, {1})
            # Other characters' reactions are visible but do not wake this bot.
            outgoing.reactions.append(Reaction('👍', [NS(id=10, name='other-bot', bot=True)]))
            await bot.on_raw_reaction_add(NS(channel_id=100, message_id=2, user_id=10, member=NS(id=10, bot=True)))
            clock[0] += 1800
            await bot.initiative_tick()
            assert len(provider.requests) == 2
            # Removal is attributed via user lookup; it also updates memory input.
            store.write_memory(100, '- memory', store.checkpoint(100))
            clock[0] += 1
            outgoing.reactions = []
            await bot.on_raw_reaction_remove(NS(channel_id=100, message_id=2, user_id=7))
            assert 'message:2: none' in store.reactions(100, {2})
            assert '<current_reactions>' in store.changes(100)[0]
            assert store.attention(100)[0] > store.attention(100)[1]
        finally:
            await bot.close()
            store.db.close()
    asyncio.run(scenario())


def test_restart_recovers_unobserved_human_messages_without_replaying_seen_chat(tmp_path):
    async def scenario():
        clock = [10000]
        provider = Scripted(response(call('finish', {})), response(call('finish', {}, 'next')))
        bot, store, channels = setup(tmp_path, provider, clock)
        try:
            await bot.on_message(human(channels[100], 'first'))
            clock[0] += 1800
            await bot.initiative_tick()
            await bot.close()
            store.db.close()
            # Created on Discord while the process was offline, no on_message event.
            human(channels[100], 'arrived while offline')
            clock[0] += 1800
            bot, store, _ = setup(tmp_path, provider, clock, channels)
            # A newer bot event must not advance past unseen offline humans.
            await bot.on_message(human(channels[100], 'new bot chatter', actor=9))
            await bot.initiative_tick()
            assert len(provider.requests) == 2
            assert 'arrived while offline' in str(provider.requests[-1])
            clock[0] += 1800
            await bot.initiative_tick()
            assert len(provider.requests) == 2
        finally:
            await bot.close()
            store.db.close()
    asyncio.run(scenario())


def test_queued_human_ping_takes_over_and_is_not_repeated_by_next_check(tmp_path):
    async def scenario():
        clock = [10000]
        class Provider:
            def __init__(self):
                self.started, self.resume, self.steps = asyncio.Event(), asyncio.Event(), 0
            async def complete(self, messages, **kwargs):
                self.steps += 1
                if self.steps == 1:
                    self.started.set()
                    await self.resume.wait()
                    return response(call('finish', {'text': 'Older check-in'}))
                return FINAL
        provider = Provider()
        bot, store, channels = setup(tmp_path, provider, clock)
        try:
            await bot.on_message(human(channels[100]))
            clock[0] += 1800
            tick = asyncio.create_task(bot.initiative_tick())
            await provider.started.wait()
            direct = asyncio.create_task(bot.on_message(human(channels[100], '<@9> answer this instead', ping=True)))
            await asyncio.sleep(0)
            provider.resume.set()
            await asyncio.gather(tick, direct)
            assert provider.steps == 2
            assert [call['content'] for call in channels[100].sent_calls] == ['Done.']
            assert store.attention(100)[:2] == (2, 2)
            clock[0] += 1800
            await bot.initiative_tick()
            assert provider.steps == 2
        finally:
            await bot.close()
            store.db.close()
    asyncio.run(scenario())


def test_reaction_delivery_failure_records_partial_actions_and_does_not_advance_context(tmp_path):
    async def scenario():
        clock = [10000]
        provider = Scripted(response(call('finish', {'text': 'A reply', 'reactions': [{'message_id': 1, 'emoji': '❤️'}]})))
        bot, store, channels = setup(tmp_path, provider, clock)
        source = human(channels[100], '<@9> react please', ping=True)
        async def fail(emoji): raise RuntimeError('Reaction failed')
        source.add_reaction = fail
        try:
            await bot.on_message(source)
            assert store.db.execute('SELECT state FROM jobs').fetchall() == [('failed',)]
            assert store.db.execute('SELECT state,kind FROM deliveries ORDER BY part').fetchall() == [
                ('sent', 'message'), ('started', 'reaction')]
            assert store.previous(100) is None
            assert channels[100].sent_calls[0]['content'] == 'A reply'
        finally:
            await bot.close()
            store.db.close()
    asyncio.run(scenario())


def test_finish_silence_does_not_upload_previously_staged_files(tmp_path):
    async def scenario():
        clock = [10000]
        provider = Scripted(response(call('write_file', {'path': 'draft.txt', 'content': 'draft'})),
                            response(call('return_file', {'path': 'draft.txt'}, 'stage')),
                            response(call('finish', {}, 'quiet')))
        bot, store, channels = setup(tmp_path, provider, clock)
        try:
            await bot.on_message(human(channels[100], '<@9> consider this', ping=True))
            assert not channels[100].sent_calls
            assert store.db.execute('SELECT state FROM jobs').fetchall() == [('complete',)]
        finally:
            await bot.close()
            store.db.close()
    asyncio.run(scenario())


def test_first_enable_baselines_old_chat_and_allowed_users_gate_wakeups(tmp_path):
    async def scenario():
        clock, provider = [10000], Scripted()
        bot, store, channels = setup(tmp_path, provider, clock)
        bot.config['allowed_user_ids'] = {7}
        try:
            human(channels[100], 'old conversation before enable')
            await bot.initiative_tick()
            assert store.attention(100)[3] == 1
            clock[0] += 1800
            await bot.on_message(human(channels[100], 'excluded user', actor=8))
            await bot.initiative_tick()
            assert not provider.requests
        finally:
            await bot.close()
            store.db.close()
    asyncio.run(scenario())


def test_new_activity_during_checkin_remains_pending_and_suppresses_stale_output(tmp_path):
    async def scenario():
        clock = [10000]
        class Provider:
            def __init__(self): self.steps = 0
            async def complete(self, messages, **kwargs):
                self.steps += 1
                if self.steps == 1:
                    await bot.on_message(human(channels[100], 'new human activity while thinking'))
                    return response(call('finish', {'text': 'Stale reply'}))
                return response(call('finish', {}, 'next'))
        provider = Provider()
        bot, store, channels = setup(tmp_path, provider, clock)
        try:
            await bot.on_message(human(channels[100]))
            clock[0] += 1800
            await bot.initiative_tick()
            assert not channels[100].sent_calls
            assert store.attention(100)[:2] == (2, 1)
            assert store.db.execute('SELECT state FROM jobs').fetchall() == [('failed',)]
            clock[0] += 1800
            await bot.initiative_tick()
            assert provider.steps == 2
            assert store.attention(100)[:2] == (2, 2)
        finally:
            await bot.close()
            store.db.close()
    asyncio.run(scenario())


def test_checkin_cancel_does_not_stop_scheduler_and_next_activity_can_run(tmp_path):
    async def scenario():
        clock = [10000]
        class Provider:
            def __init__(self): self.started = asyncio.Event(); self.steps = 0
            async def complete(self, messages, **kwargs):
                self.steps += 1
                if self.steps == 1:
                    self.started.set()
                    await asyncio.Event().wait()
                return response(call('finish', {}, 'next'))
        provider = Provider()
        bot, store, channels = setup(tmp_path, provider, clock)
        try:
            await bot.on_message(human(channels[100]))
            clock[0] += 1800
            tick = asyncio.create_task(bot.initiative_tick())
            await provider.started.wait()
            await bot.on_message(human(channels[100], '<@9> cancel', ping=True))
            await tick
            assert store.db.execute('SELECT state FROM jobs').fetchall() == [('cancelled',)]
            assert not bot.active and not bot.tasks
            await bot.on_message(human(channels[100], 'fresh chat'))
            clock[0] += 1800
            await bot.initiative_tick()
            assert provider.steps == 2
        finally:
            await bot.close()
            store.db.close()
    asyncio.run(scenario())


def test_silent_reads_keep_continuity_with_no_prior_bot_message():
    old = Window([Entry(i, 7 if i % 2 else 8, 'user', 'now', 'chat') for i in range(1, 21)], last_seen_id=20)
    new = old.messages + [Entry(21, 7, 'user', 'now', 'new chat')]
    result = select_window(new, old)
    assert result.messages[:20] == old.messages
    assert result.gap_before is None


def test_offline_catchup_checks_newest_activity_within_fetch_bound(tmp_path):
    async def scenario():
        clock, provider = [10000], Scripted(response(call('finish', {})))
        bot, store, channels = setup(tmp_path, provider, clock)
        bot.config['context'].update(soft_chunks=2, hard_chunks=4, max_fetch_messages=8)
        channel = channels[100]
        try:
            old = human(channel, 'old chat')
            store.baseline_attention(100, old.id)
            for _ in range(10):
                human(channel, 'offline bot chatter', actor=9)
            fresh = human(channel, 'newest human activity')
            human(channel, 'newest bot chatter', actor=9)
            clock[0] += 1800
            await bot.initiative_tick()
            assert len(provider.requests) == 1
            assert 'newest human activity' in str(provider.requests[0])
            assert store.attention(100)[:2] == (1, 1)
            assert store.previous(100).last_seen_id > fresh.id
        finally:
            await bot.close()
            store.db.close()
    asyncio.run(scenario())


def test_shutdown_cancels_scheduler_and_its_active_checkin(tmp_path):
    async def scenario():
        clock = [10000]
        class Provider:
            def __init__(self): self.started = asyncio.Event()
            async def complete(self, messages, **kwargs):
                self.started.set()
                await asyncio.Event().wait()
        provider = Provider()
        bot, store, channels = setup(tmp_path, provider, clock)
        try:
            await bot.on_message(human(channels[100]))
            clock[0] += 1800
            bot.memory_task = asyncio.create_task(bot.initiative_tick())
            await provider.started.wait()
            child = next(iter(bot.tasks))
            await bot.close()
            assert bot.memory_task.cancelled() and child.cancelled()
            assert not bot.active and not bot.tasks
            assert not channels[100].sent_calls
            assert store.db.execute('SELECT state FROM jobs').fetchall() == [('cancelled',)]
        finally:
            await bot.close()
            store.db.close()
    asyncio.run(scenario())


def test_finish_terminal_tool_validates_scope_and_preserves_silence(tmp_path):
    current, registry = job(tmp_path, max_calls=1, max_steps=1)
    provider = Scripted(response(call('finish', {})))
    result = asyncio.run(Runtime(provider, registry, current.settings).run([{'role': 'user', 'content': 'chat'}], current))
    assert result.answer == ''
    assert current.decision['reactions'] == []
    assert current.store.db.execute('SELECT count(*) FROM tool_receipts').fetchone()[0] == 1
    assert provider.options[0]['tool_choice'] == 'auto'
    assert [t['function']['name'] for t in provider.options[0]['tools']] == ['finish']
    with pytest.raises(ValueError, match='target'):
        current.finish(reactions=[{'message_id': 999, 'emoji': '❤️'}])
    with pytest.raises(ValueError, match='target'):
        current.finish(text='reply', reply_to=999)
    current.write_file('draft.txt', 'draft')
    current.return_file('draft.txt')
    current.finish()
    assert current.decision['send_files'] is False
    current.finish(text='')
    assert current.decision['send_files'] is True


def test_normal_and_burst_reactors_are_distinct_and_large_lists_marked_partial():
    source = NS(reactions=[Reaction('❤️', [NS(id=7, name='bevvy', bot=False)], [NS(id=9, name='faust', bot=True)])])
    snapshot = asyncio.run(reaction_snapshot(source))
    assert {m['kind'] for m in snapshot['members']} == {'normal', 'burst'}
    assert not snapshot['incomplete']
    source.reactions = [Reaction('👍', [NS(id=i, name='user', bot=False) for i in range(150)])]
    snapshot = asyncio.run(reaction_snapshot(source))
    assert len(snapshot['members']) == 100
    assert snapshot['incomplete']


def test_rejected_emoji_does_not_fail_a_delivered_reply(tmp_path):
    async def scenario():
        clock = [10000]
        provider = Scripted(response(call('finish', {'text': 'A reply', 'reactions': [{'message_id': 1, 'emoji': ':nope:'}]})))
        bot, store, channels = setup(tmp_path, provider, clock)
        source = human(channels[100], '<@9> react please', ping=True)
        async def reject(emoji):
            raise discord.HTTPException(NS(status=400, reason='Bad Request'), 'Unknown Emoji')
        source.add_reaction = reject
        try:
            await bot.on_message(source)
            assert store.db.execute('SELECT state FROM jobs').fetchall() == [('complete',)]
            assert store.db.execute('SELECT state,kind FROM deliveries ORDER BY part').fetchall() == [
                ('sent', 'message'), ('failed', 'reaction')]
            assert store.previous(100) is not None
            assert [c['content'] for c in channels[100].sent_calls] == ['A reply']
        finally:
            await bot.close()
            store.db.close()
    asyncio.run(scenario())
