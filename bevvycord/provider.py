import logging
import hashlib
import json
import os
import httpx
from .recovery import ProviderError, TurnError

log = logging.getLogger(__name__)


def request_fingerprints(messages, tools=None, tool_choice=None):
    """Content-free diagnostics; hashes identify changes, not provider cache keys."""
    def fingerprint(value):
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        return hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:32]

    sections = {}
    for message in messages:
        content = message.get('content')
        section = 'working'
        if message.get('role') == 'system':
            section = 'system'
        elif message.get('role') == 'user' and isinstance(content, str):
            section = next((name for name in ('conversation', 'memory', 'reactions', 'job', 'deferred_checkin')
                            if content.startswith('<' + name + '>')), 'context')
        sections.setdefault(section, []).append(message)
    result = {'sections': {name: {'hash': fingerprint(group), 'messages': len(group),
                                  'content_chars': sum(len(m['content']) for m in group
                                                       if isinstance(m.get('content'), str))}
                           for name, group in sections.items()},
              'tools': fingerprint({'tools': tools or [], 'tool_choice': tool_choice})}
    result['message_hashes'] = [fingerprint(message) for message in messages]
    conversation = sections.get('conversation')
    if conversation:
        result['conversation_head'] = fingerprint(conversation[0])
    return result


class Provider:
    def __init__(self, config, environment=None):
        self.config = config
        key = (os.environ if environment is None else environment).get(config.get('api_key_env', 'DEEPSEEK_API_KEY'))
        if not key:
            raise ValueError('Provider API-key environment variable is unset')
        self.client = httpx.AsyncClient(
            base_url=config['base_url'].rstrip('/') + '/',
            headers={'Authorization': f'Bearer {key}'},
            timeout=config.get('timeout_seconds', 180),
        )

    async def generate(self, messages, model=None, parameters=None):
        choice = await self.complete(messages, model=model, parameters=parameters)
        if choice.get('finish_reason') not in ('stop', 'end_turn'):
            raise TurnError('Provider did not complete the answer; adjust output limits')
        content = choice['message'].get('content')
        if not isinstance(content, str) or not content.strip():
            raise TurnError('Provider returned an empty answer')
        return content

    async def complete(self, messages, model=None, parameters=None, tools=None, tool_choice=None):
        payload = {
            **(self.config.get('parameters', {}) if parameters is None else parameters),
            'model': model or self.config['model'], 'messages': messages, 'stream': False,
        }
        if tools:
            payload['tools'] = tools
        if tool_choice is not None:
            payload['tool_choice'] = tool_choice
        response = await self.client.post('chat/completions', json={
            **payload,
        })
        # Avoid logging HTTP bodies, headers, or exception objects containing secrets.
        if response.is_error:
            raise ProviderError(response.status_code)
        data = response.json()
        choice = data['choices'][0]
        usage = data.get('usage', {})
        from .activity import current
        event = current.get()
        if event is not None:
            recorded = {key: usage.get(key) for key in (
                'prompt_tokens', 'completion_tokens', 'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens')}
            recorded['model'] = payload['model']
            recorded['fingerprints'] = request_fingerprints(messages, tools, tool_choice)
            for key in ('model', 'system_fingerprint'):
                if isinstance(data.get(key), str):
                    recorded['served_model' if key == 'model' else key] = data[key]
            event['usage'].append(recorded)
        log.debug('Usage: prompt=%s completion=%s cache_hit=%s cache_miss=%s',
                 usage.get('prompt_tokens'), usage.get('completion_tokens'),
                 usage.get('prompt_cache_hit_tokens'), usage.get('prompt_cache_miss_tokens'))
        # The runtime replays the complete assistant message internally, including
        # reasoning_content. generate() still exposes only final text to memory.
        return choice

    async def close(self):
        await self.client.aclose()
