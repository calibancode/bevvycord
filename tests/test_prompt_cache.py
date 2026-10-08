import asyncio
import copy
from types import SimpleNamespace as NS

from bevvycord.bot import CharacterBot
from bevvycord.context import Message, Window, chunks
from bevvycord.prompts import reply_messages
from bevvycord.storage import Store
from test_agent_flow import Channel, Message as DiscordMessage, config
from test_runtime import FINAL


def entry(i, actor=7, text=None):
    return Message(i, actor, f'speaker-{actor}', '2026-10-07', text or f'message {i}', actor == 9)


def test_transcript_prefix_survives_growth_of_same_speaker_chunk():
    old = Window([entry(1), entry(2)])
    growing = Window(old.messages + [entry(3), entry(4, 9), entry(5)])
    first = reply_messages('Rowan', 9, old, '- A memory')
    second = reply_messages('Rowan', 9, growing, '- A memory')
    assert second[:len(first)] == first
    assert len(chunks(old.messages)) == 1
    assert len(chunks(growing.messages)) == 3
    assert all(m['role'] == 'user' for m in second[1:])
    assert '(bot:9)' in second[-2]['content']
    assert second[-1]['content'].count('message 5') == 1


def test_memory_and_gap_are_separate_source_messages():
    window = Window([entry(1), entry(10), entry(11)], gap_before=10)
    first = reply_messages('Rowan', 9, window, '- old')
    second = reply_messages('Rowan', 9, window, '- new')
    assert first[0] == second[0]
    assert first[1] != second[1]
    assert first[2:] == second[2:]
    assert first[3]['content'].startswith('[Context note:')
    assert sum('[Context note:' in m['content'] for m in first) == 1
    assert '(empty)' in reply_messages('Rowan', 9, window, '')[1]['content']
    assert reply_messages('Rowan', 9, window)[1]['content'].startswith('<conversation>')


def test_edit_and_window_reset_invalidate_only_the_expected_prefix():
    old = reply_messages('Rowan', 9, Window([entry(1), entry(2), entry(3)]))
    edited = reply_messages('Rowan', 9, Window([entry(1), entry(2, text='corrected'), entry(3)]))
    assert old[:2] == edited[:2]
    assert old[2] != edited[2]
    reset = reply_messages('Rowan', 9, Window([entry(10), entry(11)]))
    assert old[0] == reset[0]
    assert old[1] != reset[1]


def test_actual_bot_requests_keep_history_prefix_and_isolate_job_details(tmp_path):
    class Provider:
        def __init__(self): self.requests = []
        async def complete(self, messages, **kwargs):
            self.requests.append(copy.deepcopy(messages))
            return FINAL
    async def scenario():
        cfg, provider, channel = config(), Provider(), Channel()
        store = Store(tmp_path, 'rowan')
        store.memory_path(100).write_text('- Fixed memory')
        bot = CharacterBot(cfg, store, provider)
        bot._connection.user = NS(id=9)
        try:
            await bot.on_message(DiscordMessage(channel, '<@9> first'))
            await bot.on_message(DiscordMessage(channel, '<@9> second'))
            first, second = provider.requests
            assert first[-1]['content'].startswith('<job>')
            assert second[-1]['content'].startswith('<job>')
            assert first[-1] != second[-1]
            assert second[:len(first) - 1] == first[:-1]
            assert 'Job ID:' not in str(first[:-1])
            assert second[-2]['content'].count('<@9> second') == 1
            assert '(bot:9)' in second[-3]['content']
            archive, _, _ = store.archive(100)
            assert '<job>' not in archive
        finally:
            await bot.close()
            store.db.close()
    asyncio.run(scenario())


def test_segmented_context_still_enforces_total_prompt_size(tmp_path):
    async def scenario():
        cfg, channel = config(), Channel()
        cfg['context']['max_prompt_chars'] = 100
        store = Store(tmp_path, 'rowan')
        bot = CharacterBot(cfg, store, NS())
        bot._connection.user = NS(id=9)
        try:
            await bot.on_message(DiscordMessage(channel, '<@9> first'))
            assert 'couldn’t complete' in list(channel.messages.values())[-1].content
            assert store.previous(100) is None
        finally:
            await bot.close()
            store.db.close()
    asyncio.run(scenario())
