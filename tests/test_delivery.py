import asyncio
from types import SimpleNamespace as NS

import pytest

from bevvycord.bot import CharacterBot
from bevvycord.discord_io import delivery_parts
from bevvycord.storage import Store
from test_agent_flow import Channel, Message, config
from test_runtime import call, response


def test_attachment_batches_respect_count_and_request_size():
    artifacts = [NS(size=8 * 1024 * 1024) for _ in range(5)]
    parts = delivery_parts('Caption', artifacts)
    assert [len(files) for _, files in parts] == [3, 2]
    assert [text for text, _ in parts] == ['Caption', '']
    assert [file for _, files in parts for file in files] == artifacts
    assert [len(files) for _, files in delivery_parts('Caption', [NS(size=1) for _ in range(11)])] == [10, 1]
    with pytest.raises(ValueError, match='request size'):
        delivery_parts('Caption', [NS(size=25 * 1024 * 1024)])


@pytest.mark.parametrize('fail_continuation', [False, True])
def test_long_reply_attaches_files_once_and_only_references_first_message(tmp_path, fail_continuation):
    answer = ('A useful sentence.\n' * 250) + 'Complete.'
    class Provider:
        def __init__(self): self.step = 0
        async def complete(self, messages, **kwargs):
            self.step += 1
            if self.step == 1:
                return response(call('write_file', {'path': 'a.txt', 'content': 'A'}, 'a'),
                                call('write_file', {'path': 'b.txt', 'content': 'B'}, 'b'))
            if self.step == 2:
                return response(call('return_file', {'path': 'a.txt'}, 'attach-a'),
                                call('return_file', {'path': 'b.txt'}, 'attach-b'))
            return {'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': answer}}
    async def scenario():
        cfg, store, channel = config(), Store(tmp_path, 'rowan'), Channel()
        cfg['memory']['enabled'] = False
        if fail_continuation:
            async def fail(**kwargs): raise RuntimeError('Continuation failed')
            channel.send = fail
        bot = CharacterBot(cfg, store, Provider())
        bot._connection.user = NS(id=9)
        trigger = Message(channel, '<@9> write a long reply with two files')
        try:
            await bot.on_message(trigger)
            assert channel.files == [('a.txt', b'A'), ('b.txt', b'B')]
            assert channel.sent_calls[0]['reference'] == trigger.id
            assert all(f.fp.closed for f in channel.sent_calls[0]['files'])
            states = store.db.execute('SELECT state FROM jobs').fetchall()
            receipts = store.db.execute('SELECT state,message_id FROM deliveries ORDER BY part').fetchall()
            outgoing = [m for m in channel.messages.values() if m.author.bot]
            if fail_continuation:
                assert states == [('failed',)]
                assert [row[0] for row in receipts] == ['sent', 'started']
                assert not store.previous(100)
                assert outgoing[0].attachments and 'couldn’t complete' in outgoing[-1].content
            else:
                assert states == [('complete',)]
                assert len(outgoing) == 3
                assert ''.join(m.content for m in outgoing) == answer
                assert all(len(m.content) <= 2000 for m in outgoing)
                assert len(outgoing[0].attachments) == 2
                assert all(not m.attachments and m.reference is None for m in outgoing[1:])
                assert all(call['reference'] is None for call in channel.sent_calls[1:])
                assert [row[0] for row in receipts] == ['sent'] * 3
                assert [row[1] for row in receipts] == [m.id for m in outgoing]
                assert store.previous(100).last_response_id == outgoing[-1].id
                archive, _, _ = store.archive(100)
                assert archive.count('[Attachment: a.txt; content not read]') == 1
                assert archive.count('[Attachment: b.txt; content not read]') == 1
        finally:
            await bot.close()
            store.db.close()
    asyncio.run(scenario())
