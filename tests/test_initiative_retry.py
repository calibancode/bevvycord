import asyncio
import copy

from test_initiative import setup, human
from test_runtime import response, call


def test_interruption_retries_fresh_context_and_preserves_work(tmp_path, caplog):
    async def scenario():
        clock = [10000]
        class Provider:
            def __init__(self): self.requests = []
            async def complete(self, messages, **kwargs):
                self.requests.append(copy.deepcopy(messages))
                step = len(self.requests)
                if step == 1:
                    return response(call('write_file', {'path': 'draft.txt', 'content': 'work survives'}))
                if step == 2:
                    clock[0] += 10
                    await bot.on_message(human(channels[100], 'new human thought'))
                    return response(call('finish', {'text': 'stale reply'}, 'finish'))
                if step == 3:
                    old = store.db.execute("SELECT id FROM jobs WHERE state='deferred'").fetchone()[0]
                    assert '<deferred_checkin>' in str(messages)
                    assert 'new human thought' in str(messages)
                    return response(call('open_job', {'job_id': old}))
                clock[0] += 10
                return response(call('finish', {'text': 'fresh reply'}, 'finish'))
        provider = Provider()
        bot, store, channels = setup(tmp_path, provider, clock)
        try:
            await bot.on_message(human(channels[100]))
            clock[0] += 1800
            await bot.initiative_tick()
            assert [item['content'] for item in channels[100].sent_calls] == ['fresh reply']
            assert store.db.execute('SELECT state FROM jobs ORDER BY created').fetchall() == [('deferred',), ('complete',)]
            assert store.db.execute('SELECT state FROM activity ORDER BY started').fetchall() == [('deferred',), ('complete',)]
            assert store.db.execute("SELECT COUNT(*) FROM tool_receipts WHERE name='write_file'").fetchone()[0] == 1
            assert store.attention(100)[:2] == (2, 2)
            assert store.attention(100)[2] == clock[0]
            assert not bot.deferred_jobs and not bot.initiative_interruptions
            assert not bot.coordinator.channels[100]['busy']
            assert 'Reply failed' not in caplog.text
        finally:
            await bot.close()
            store.db.close()
    asyncio.run(scenario())


def test_only_sustained_interruptions_back_off_and_leave_activity_pending(tmp_path, monkeypatch):
    async def scenario():
        clock, waits = [10000], []
        class Provider:
            def __init__(self): self.steps = 0
            async def complete(self, messages, **kwargs):
                self.steps += 1
                clock[0] += 1
                await bot.on_message(human(channels[100], f'interruption {self.steps}'))
                return response(call('finish', {'text': 'obsolete'}))
        provider = Provider()
        bot, store, channels = setup(tmp_path, provider, clock)
        async def sleep(seconds):
            waits.append(seconds)
            assert not bot.coordinator.channels[100]['busy']
            assert not bot.locks[100].locked()
        monkeypatch.setattr('bevvycord.bot.asyncio.sleep', sleep)
        try:
            await bot.on_message(human(channels[100]))
            clock[0] += 1800
            await bot.initiative_tick()
            assert provider.steps == 5
            assert waits == [15, 30, 60]
            assert not channels[100].sent_calls
            revision, seen, last_check, _ = store.attention(100)
            assert revision == seen + 1
            assert last_check == 10000  # No new normal interval from a deferral.
            assert bot.initiative_interruptions[100] == 5
        finally:
            await bot.close()
            store.db.close()
    asyncio.run(scenario())
