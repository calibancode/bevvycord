"""One text-only recovery request; never replay uncertain actions or raw errors."""
import json

import discord
import httpx


class TurnError(RuntimeError):
    """Harness-authored explanation that is safe to show to the model."""


class ProviderError(TurnError):
    def __init__(self, status):
        self.status = status
        super().__init__(f'Model service returned HTTP {status}')


def explanation(error):
    if isinstance(error, TurnError):
        return str(error)
    if isinstance(error, discord.HTTPException):
        return f'Discord could not complete an operation (HTTP {error.status}, code {error.code})'
    if isinstance(error, httpx.TimeoutException):
        return 'The model request timed out.'
    if isinstance(error, httpx.RequestError):
        return f'A network request failed ({type(error).__name__}).'
    if isinstance(error, TimeoutError):
        return 'The turn reached its time limit.'
    # Exceptions from libraries can include credentials, URLs or local paths.
    known = {
        'Context exceeds context.max_prompt_chars; no conversation was silently truncated':
            'The conversation exceeded the configured context-size limit.',
        'The triggering message changed during generation; please invoke again':
            'The request was edited during generation; the original output was discarded.',
        'The reply target changed during generation':
            'The selected reply target was edited during generation.',
        'A reaction target changed during generation':
            'A selected reaction target was edited during generation.',
    }
    return known.get(str(error), f'The turn was interrupted by an internal error ({type(error).__name__}).')


async def recover(provider, messages, job, error, max_chars):
    if isinstance(error, ProviderError) and error.status in (401, 403):
        raise error  # The same credentials cannot generate an explanation.
    receipts = {}
    if job:
        receipts = {
            'job': job.id,
            'tools': [{'tool': name, 'state': 'returned' if result is not None else 'uncertain'}
                      for name, result in job.store.db.execute(
                          'SELECT name,result FROM tool_receipts WHERE job=? ORDER BY rowid', (job.id,))],
            'deliveries': [{'kind': kind, 'state': state}
                           for kind, state in job.store.db.execute(
                               'SELECT kind,state FROM deliveries WHERE job=? ORDER BY part', (job.id,))],
        }
    note = {'role': 'user', 'content': '<turn_error>\n' + explanation(error) + '\n'
            + json.dumps(receipts, ensure_ascii=False)
            + '\nExplain what went wrong in your own voice. This is a final text response; '
              'no actions will be retried.\n</turn_error>'}
    candidates = [getattr(job, 'recovery_messages', None), messages]
    context = next(([*candidate, note] for candidate in candidates if candidate
                    and len(json.dumps([*candidate, note], ensure_ascii=False)) <= max_chars), None)
    if context is None:
        raise TurnError('No bounded recovery context available')
    if hasattr(provider, 'complete'):
        choice = await provider.complete(context, tool_choice='none')
        raw = choice.get('message', {})
        text = raw.get('content')
        if choice.get('finish_reason') not in ('stop', 'end_turn') or raw.get('tool_calls'):
            raise TurnError('Model did not complete a text-only recovery')
    else:
        text = await provider.generate(context)
    if not isinstance(text, str) or not text.strip():
        raise TurnError('Model returned no recovery explanation')
    return text
