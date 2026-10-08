import logging
import os
import httpx
from .recovery import ProviderError, TurnError

log = logging.getLogger(__name__)


class Provider:
    def __init__(self, config):
        self.config = config
        key = os.environ.get(config.get('api_key_env', 'DEEPSEEK_API_KEY'))
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
            event['usage'].append({key: usage.get(key) for key in (
                'prompt_tokens', 'completion_tokens', 'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens')})
        log.debug('Usage: prompt=%s completion=%s cache_hit=%s cache_miss=%s',
                 usage.get('prompt_tokens'), usage.get('completion_tokens'),
                 usage.get('prompt_cache_hit_tokens'), usage.get('prompt_cache_miss_tokens'))
        # The runtime replays the complete assistant message internally, including
        # reasoning_content. generate() still exposes only final text to memory.
        return choice

    async def close(self):
        await self.client.aclose()
