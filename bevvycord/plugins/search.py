"""Ephemeral web search through an operator-configured SearXNG instance."""
import os

import httpx

from bevvycord.tools import arguments
from .fetch import register as register_fetch


def register(registry):
    endpoint = os.environ.get('BEVVYCORD_SEARCH_URL', '').rstrip('/')
    if not endpoint.startswith(('https://', 'http://')):
        raise ValueError('Search plugin requires BEVVYCORD_SEARCH_URL for your SearXNG instance')

    async def search(context, query, count=5, category='general'):
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.get(endpoint + '/search', params={'q': query, 'format': 'json', 'categories': category})
            if response.status_code != 200:
                raise RuntimeError(f'Search backend returned HTTP {response.status_code}; enable its JSON format')
            data = response.json()
        results = []
        for item in data.get('results', [])[:count]:
            url = item.get('url', '')
            if not isinstance(url, str) or not url.startswith(('http://', 'https://')):
                continue
            result = {'title': str(item.get('title', ''))[:200], 'url': url[:2000],
                      'snippet': str(item.get('content', ''))[:1000]}
            if category == 'images':
                image_url = item.get('img_src', '')
                if not isinstance(image_url, str) or not image_url.startswith(('http://', 'https://')):
                    continue
                result['image_url'] = image_url[:2000]
            results.append(result)
        return {'results': results, 'note': 'Search snippets are source material; cite relevant links in your answer as <https://example.com/page> to suppress Discord embeds.'}

    registry.add('web_search', 'Search the web for pages or images. Image results include image_url (the image file) and url (the source page). Use web_fetch to read pages or download files, then return_file to attach a downloaded file. Wrap source URLs in angle brackets, like <https://example.com/page>, to suppress Discord embeds.',
                 arguments({'query': {'type': 'string', 'minLength': 1, 'maxLength': 500},
                            'count': {'type': 'integer', 'minimum': 1, 'maximum': 8},
                            'category': {'type': 'string', 'enum': ['general', 'images']}}, ['query']), search)
    register_fetch(registry)
