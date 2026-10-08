"""Bounded tool-call turns; scratch messages never enter the chat archive."""
import asyncio
from dataclasses import dataclass
import json
from .recovery import TurnError


@dataclass
class Result:
    answer: str
    job: object


def tool_name(call):
    function = call.get('function') if isinstance(call, dict) else None
    return function.get('name') if isinstance(function, dict) else None


class Runtime:
    def __init__(self, provider, registry, settings):
        self.provider, self.registry, self.settings = provider, registry, settings

    async def run(self, messages, job):
        async with asyncio.timeout(self.settings['turn_seconds']):
            return await self._run(messages, job)

    async def _run(self, messages, job):
        scratch = [dict(m) for m in messages]
        scratch.append({'role': 'user', 'content': '<job>\n' + job.environment() + '\n</job>' + job.library_context()})
        job.recovery_messages = [dict(m) for m in scratch]
        count = 0
        for step in range(self.settings['max_steps']):
            finishing = step == self.settings['max_steps'] - 1 or count >= self.settings['max_calls']
            options = {'tools': self.registry.definitions()}
            if finishing:
                scratch.append({'role': 'user', 'content':
                                'Your tool budget for this turn is exhausted. Give your final reply now using '
                                'the work already completed, or use finish for reactions or silence.'})
                if 'finish' in self.registry.tools:
                    # DeepSeek thinking mode accepts auto/none, not forced tools.
                    options['tools'] = [tool for tool in options['tools'] if tool['function']['name'] == 'finish']
                    options['tool_choice'] = 'auto'
                else:
                    options['tool_choice'] = 'none'
            if len(json.dumps(scratch, ensure_ascii=False)) > self.settings['max_working_chars']:
                raise TurnError('Tool working context limit reached')
            choice = await self.provider.complete(scratch, **options)
            raw = choice.get('message', {})
            calls = raw.get('tool_calls')
            if not calls:
                content = raw.get('content')
                if choice.get('finish_reason') not in ('stop', 'end_turn') or not isinstance(content, str) or not content.strip():
                    raise TurnError('Provider did not complete the final answer')
                return Result(content, job)
            if finishing and (not isinstance(calls, list) or len(calls) != 1
                              or tool_name(calls[0]) != 'finish'):
                # Budget is spent: salvage text written alongside the stray calls.
                content = raw.get('content')
                if isinstance(content, str) and content.strip():
                    return Result(content, job)
                raise TurnError('Provider requested work tools during the final reply; no further tools were executed')
            if (choice.get('finish_reason') not in ('tool_calls', 'stop') or not isinstance(calls, list)
                    or len(calls) > self.settings['max_calls'] + 1):
                raise TurnError('Malformed provider tool response')
            # Preserve DeepSeek reasoning_content verbatim on assistant replay.
            # It is never written to the transcript, receipts or Discord.
            seen = set()
            for call in calls:
                call_id = call.get('id') if isinstance(call, dict) else None
                if not isinstance(call_id, str) or not call_id or len(call_id) > 200 or call_id in seen:
                    raise TurnError('Malformed or repeated tool call ID in one response')
                seen.add(call_id)
            assistant = {k: v for k, v in raw.items() if k in ('role', 'content', 'tool_calls', 'reasoning_content')}
            assistant['role'] = 'assistant'
            scratch.append(assistant)
            for index, call in enumerate(calls):
                call_id = call.get('id')
                terminal = tool_name(call) == 'finish'
                if not terminal:
                    count += 1
                if terminal and index != len(calls) - 1:
                    result = {'error': 'finish must be the last call in a batch'}
                elif not terminal and count > self.settings['max_calls']:
                    result = {'error': 'Tool call limit reached; finish with the work already completed'}
                else:
                    result = await self.dispatch(job, call)
                encoded = json.dumps(result, ensure_ascii=False)
                if len(encoded) > self.settings['output_chars'] + 2000:
                    encoded = json.dumps({'error': 'Tool result exceeded output limit', 'preview': encoded[:self.settings['output_chars']]}, ensure_ascii=False)
                scratch.append({'role': 'tool', 'tool_call_id': call_id, 'content': encoded})
            # Only save complete protocol exchanges. A malformed response or an
            # interrupted batch must not create orphaned tool calls on recovery.
            job.recovery_messages = [dict(m) for m in scratch]
            if job.decision is not None:
                return Result(job.decision['text'], job)
        raise TurnError('Model step limit reached without a final answer')

    async def dispatch(self, job, call):
        function = call.get('function', {})
        if not isinstance(function, dict):
            return {'error': 'Malformed tool call'}
        name, raw = function.get('name'), function.get('arguments')
        if call.get('type', 'function') != 'function' or not isinstance(name, str) or not isinstance(raw, str) or len(raw) > self.settings['max_working_chars']:
            return {'error': 'Malformed tool call'}
        try:
            def invalid_constant(value):
                raise ValueError('Non-finite JSON number')
            args = json.loads(raw, parse_constant=invalid_constant)
        except (ValueError, RecursionError):
            return {'error': 'Arguments must be valid JSON'}
        canonical = json.dumps(args, sort_keys=True, ensure_ascii=False)
        previous = job.store.db.execute('SELECT name,arguments,result FROM tool_receipts WHERE job=? AND call_id=?', (job.id, call['id'])).fetchone()
        if previous:
            if previous[:2] != (name, canonical):
                return {'error': 'Tool call ID was reused with different arguments'}
            if previous[2] is None:
                return {'error': 'Previous execution was interrupted; its side effect is uncertain and will not be repeated'}
            return json.loads(previous[2])
        job.store.receipt_start(job.id, call['id'], name, canonical)
        job.call_id = call['id']
        result = await self.registry.call(name, args, job)
        try:
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            result = {'error': 'Tool returned a result that is not valid JSON'}
            encoded = json.dumps(result)
        if len(encoded) > self.settings['output_chars'] + 2000:
            result = {'error': 'Tool result exceeded output limit', 'preview': encoded[:self.settings['output_chars']]}
        job.store.receipt_finish(job.id, call['id'], json.dumps(result, ensure_ascii=False))
        return result
