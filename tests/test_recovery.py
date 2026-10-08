import asyncio
import copy

from bevvycord.recovery import ProviderError, TurnError
from test_review_fixes import make_bot
from test_agent_flow import Channel, Message
from test_runtime import Scripted, call, response


EXPLAIN = {'finish_reason': 'stop', 'message': {'content': 'Aw, that didn’t work. Can we try something smaller?'}}


class Provider(Scripted):
    async def complete(self, messages, **kwargs):
        if isinstance(self.replies[0], Exception):
            # Keep exactly the same recording behavior for failed requests.
            error = self.replies.pop(0)
            self.requests.append(copy.deepcopy(messages))
            self.options.append(kwargs)
            raise error
        return await super().complete(messages, **kwargs)


def test_malformed_batch_recovers_in_character_without_executing_it(tmp_path):
    async def scenario():
        malformed = response(call('write_file', {'path': 'bad.txt', 'content': 'bad'}),
                             call('write_file', {'path': 'other.txt', 'content': 'bad'}))
        provider = Provider(malformed, EXPLAIN)
        bot, store = make_bot(tmp_path, provider)
        channel, trigger = Channel(), None
        trigger = Message(channel, '<@9> do some work')
        try:
            await bot.on_message(trigger)
            assert channel.sent_calls[0]['content'] == EXPLAIN['message']['content']
            assert len(provider.requests) == 2 and provider.options[-1] == {'tool_choice': 'none'}
            assert '<turn_error>' in provider.requests[-1][-1]['content']
            assert not any(m['role'] == 'assistant' for m in provider.requests[-1])
            assert store.db.execute('SELECT count(*) FROM tool_receipts').fetchone()[0] == 0
            assert store.db.execute('SELECT state FROM jobs').fetchall() == [('failed',)]
            assert not list(store.root.glob('jobs/*/*/work/*.txt'))
            assert store.previous(100) is None
        finally:
            await bot.close(); store.db.close()
    asyncio.run(scenario())


def test_truncated_final_replays_valid_work_but_never_uploads_staged_artifacts(tmp_path):
    async def scenario():
        provider = Provider(response(call('write_file', {'path': 'draft.txt', 'content': 'draft'})),
                            response(call('return_file', {'path': 'draft.txt'}, 'stage')),
                            {'finish_reason': 'length', 'message': {'content': 'UNFINISHED ANSWER'}}, EXPLAIN)
        bot, store = make_bot(tmp_path, provider)
        trigger = Message(Channel(), '<@9> create this file')
        try:
            await bot.on_message(trigger)
            assert len(provider.requests) == 4
            assert any(m['role'] == 'tool' for m in provider.requests[-1])
            assert 'PRIVATE INTERNAL REASONING' in str(provider.requests[-1])
            assert 'UNFINISHED ANSWER' not in str(provider.requests[-1])
            assert store.db.execute('SELECT count(*) FROM tool_receipts').fetchone()[0] == 2
            assert len(trigger.channel.sent_calls) == 1 and not trigger.channel.files
            assert trigger.channel.sent_calls[0]['content'] == EXPLAIN['message']['content']
            assert store.db.execute('SELECT state FROM jobs').fetchall() == [('failed',)]
        finally:
            await bot.close(); store.db.close()
    asyncio.run(scenario())


def test_timed_out_batch_reports_uncertain_receipt_without_orphan_tool_call_or_retry(tmp_path):
    async def scenario():
        provider = Provider(response(call('hang', {})), EXPLAIN)
        bot, store = make_bot(tmp_path, provider)
        bot.tool_settings['turn_seconds'] = .05
        calls = 0
        async def hang(context):
            nonlocal calls
            calls += 1
            await asyncio.Event().wait()
        bot.runtime.registry.add('hang', 'hang', {'type': 'object', 'properties': {}}, hang)
        trigger = Message(Channel(), '<@9> work')
        try:
            await bot.on_message(trigger)
            assert calls == 1 and len(provider.requests) == 2
            assert 'uncertain' in provider.requests[-1][-1]['content']
            assert not any(m['role'] in ('assistant', 'tool') for m in provider.requests[-1])
            assert store.db.execute('SELECT result FROM tool_receipts').fetchone()[0] is None
            assert trigger.channel.sent_calls[-1]['content'] == EXPLAIN['message']['content']
        finally:
            await bot.close(); store.db.close()
    asyncio.run(scenario())


def test_unauthorized_model_uses_fallback_without_retrying_same_credentials(tmp_path):
    async def scenario():
        provider = Provider(ProviderError(401))
        bot, store = make_bot(tmp_path, provider)
        trigger = Message(Channel(), '<@9> hello')
        try:
            await bot.on_message(trigger)
            assert len(provider.requests) == 1
            assert 'couldn’t complete' in trigger.channel.sent_calls[0]['content']
        finally:
            await bot.close(); store.db.close()
    asyncio.run(scenario())


def test_raw_library_error_does_not_reach_model_or_log(tmp_path, caplog):
    async def scenario():
        provider = Provider(ValueError('SECRET_TOKEN_IN_LIBRARY_EXCEPTION'), EXPLAIN)
        bot, store = make_bot(tmp_path, provider)
        trigger = Message(Channel(), '<@9> hello')
        try:
            await bot.on_message(trigger)
            assert 'SECRET_TOKEN' not in str(provider.requests)
            assert 'SECRET_TOKEN' not in caplog.text
            assert 'SECRET_TOKEN' not in store.db.execute('SELECT detail FROM jobs').fetchone()[0]
            assert 'internal error (ValueError)' in provider.requests[-1][-1]['content']
            assert trigger.channel.sent_calls[0]['content'] == EXPLAIN['message']['content']
        finally:
            await bot.close(); store.db.close()
    asyncio.run(scenario())


def test_recovery_cannot_call_tools_and_has_only_one_attempt(tmp_path):
    async def scenario():
        provider = Provider(TurnError('Provider returned an empty answer'),
                            response(call('write_file', {'path': 'recovery.txt', 'content': 'bad'})))
        bot, store = make_bot(tmp_path, provider)
        trigger = Message(Channel(), '<@9> hello')
        try:
            await bot.on_message(trigger)
            assert len(provider.requests) == 2
            assert store.db.execute('SELECT count(*) FROM tool_receipts').fetchone()[0] == 0
            assert 'couldn’t complete' in trigger.channel.sent_calls[0]['content']
        finally:
            await bot.close(); store.db.close()
    asyncio.run(scenario())


def test_oversized_memory_uses_bounded_character_and_request_for_explanation(tmp_path):
    async def scenario():
        provider = Provider(EXPLAIN)
        bot, store = make_bot(tmp_path, provider)
        bot.config['memory']['enabled'] = True
        bot.config['context']['max_prompt_chars'] = 3000
        store.memory_path(100).write_text('VERY_LARGE_MEMORY ' * 1000)
        trigger = Message(Channel(), '<@9> hello')
        try:
            await bot.on_message(trigger)
            assert len(provider.requests) == 1
            assert 'You are Rowan' in provider.requests[0][0]['content']
            assert 'VERY_LARGE_MEMORY' not in str(provider.requests[0])
            assert 'context-size limit' in str(provider.requests[0])
            assert trigger.channel.sent_calls[0]['content'] == EXPLAIN['message']['content']
        finally:
            await bot.close(); store.db.close()
    asyncio.run(scenario())
