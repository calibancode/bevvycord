import asyncio

import httpx
import pytest

from bevvycord.tools import Registry


def test_search_plugin_compact_results_and_configuration(monkeypatch):
    from bevvycord.plugins.search import register
    monkeypatch.delenv('BEVVYCORD_SEARCH_URL', raising=False)
    with pytest.raises(ValueError): register(Registry())
    monkeypatch.setenv('BEVVYCORD_SEARCH_URL', 'https://search.example.invalid')
    original = httpx.AsyncClient
    def transport(request):
        assert request.url.path == '/search'
        assert request.url.params['q'] == 'image manipulation'
        assert request.url.params['format'] == 'json'
        return httpx.Response(200, json={'results': [{'title': 'Official docs', 'url': 'https://example.invalid/docs', 'content': 'x' * 5000},
                                                    {'title': 'unsafe link', 'url': 'javascript:alert(1)'}]})
    monkeypatch.setattr('bevvycord.plugins.search.httpx.AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(transport), **kwargs))
    registry = Registry()
    register(registry)
    result = asyncio.run(registry.call('web_search', {'query': 'image manipulation'}, None))
    assert len(result['results']) == 1
    assert len(result['results'][0]['snippet']) == 1000
    assert 'error' in asyncio.run(registry.call('web_search', {'query': 'q', 'count': 9}, None))


def test_image_search_returns_file_and_source_urls(monkeypatch):
    from bevvycord.plugins.search import register
    monkeypatch.setenv('BEVVYCORD_SEARCH_URL', 'https://search.example.invalid')
    original = httpx.AsyncClient
    def transport(request):
        assert request.url.params['categories'] == 'images'
        return httpx.Response(200, json={'results': [
            {'title': 'Buddy', 'url': 'https://example.org/page', 'img_src': 'https://example.org/buddy.png'},
            {'url': 'https://example.org/bad', 'img_src': 'file:///etc/passwd'},
            {'url': 'https://example.org/not-an-image'}]})
    monkeypatch.setattr('bevvycord.plugins.search.httpx.AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(transport), **kwargs))
    registry = Registry()
    register(registry)
    result = asyncio.run(registry.call('web_search', {'query': 'Buddy Thunderstruck', 'category': 'images'}, None))
    assert result['results'] == [{'title': 'Buddy', 'url': 'https://example.org/page', 'snippet': '', 'image_url': 'https://example.org/buddy.png'}]
    assert '<https://' in result['note']
    assert 'web_fetch' in registry.tools
    assert 'error' in asyncio.run(registry.call('web_search', {'query': 'Buddy', 'category': 'unknown'}, None))
