import asyncio
import socket

import httpcore
import httpx
import pytest

from bevvycord.plugins import fetch as web
from test_runtime import job


def mock_web(monkeypatch, handler):
    monkeypatch.setattr(web, 'PublicTransport', lambda: httpx.MockTransport(handler))


def test_download_and_freeze_image(tmp_path, monkeypatch):
    current, _ = job(tmp_path)
    image = b'\x89PNG\r\n\x1a\nimage bytes'
    def serve(request):
        assert request.headers['accept-encoding'] == 'identity'
        assert 'authorization' not in request.headers
        assert 'cookie' not in request.headers
        return httpx.Response(200, headers={'content-type': 'image/png'}, stream=httpx.ByteStream(image))
    mock_web(monkeypatch, serve)
    result = asyncio.run(web.fetch(current, 'https://example.org/buddy'))
    assert result['content_type'] == 'image/png'
    assert result['path'].endswith('-buddy.png')
    assert current.path(result['path']).read_bytes() == image
    receipt = current.return_file(result['path'], 'buddy.png')
    assert receipt['bytes'] == len(image)
    assert current.artifacts[0].path.read_bytes() == image


def test_multichunk_download_scans_once_for_growth(tmp_path, monkeypatch):
    current, _ = job(tmp_path)
    scans = []
    original = current.check_storage
    def scan(extra=0):
        scans.append(extra)
        return original(extra)
    current.check_storage = scan
    payload = b'x' * (65536 * 5)
    mock_web(monkeypatch, lambda req: httpx.Response(200, stream=httpx.ByteStream(payload)))
    result = asyncio.run(web.fetch(current, 'https://example.org/file.bin'))
    assert current.path(result['path']).read_bytes() == payload
    assert scans == [0, 0, 0]  # initial, empty destination, final


def test_destination_entries_are_counted_before_download(tmp_path, monkeypatch):
    current, _ = job(tmp_path, workspace_files=1)
    mock_web(monkeypatch, lambda req: httpx.Response(200, stream=httpx.ByteStream(b'x')))
    with pytest.raises(ValueError, match='storage limit'):
        asyncio.run(web.fetch(current, 'https://example.org/file.bin'))
    assert not list(current.work.rglob('*.bin'))


def test_html_readable_text_and_original_saved(tmp_path, monkeypatch):
    current, _ = job(tmp_path, output_chars=20)
    html = b'<html><style>hidden style</style><h1>Buddy &amp; Darnell</h1><p>Race <b>together</b>.</p><script>secret script</script></html>'
    mock_web(monkeypatch, lambda req: httpx.Response(200, headers={'content-type': 'text/html; charset=utf-8'}, stream=httpx.ByteStream(html)))
    result = asyncio.run(web.fetch(current, 'https://example.org/page'))
    assert current.path(result['path']).read_bytes() == html
    text = current.path(result['text_path']).read_text()
    assert 'Buddy & Darnell' in text and 'together' in text
    assert 'hidden style' not in text and 'secret script' not in text
    assert result['excerpt'] == text[:20]
    assert result['excerpt_truncated']


def test_relative_redirect_and_bad_status(tmp_path, monkeypatch):
    current, _ = job(tmp_path)
    seen = []
    def serve(request):
        seen.append(str(request.url))
        if request.url.path == '/start':
            return httpx.Response(302, headers={'location': '/image.png'})
        return httpx.Response(200, headers={'content-type': 'image/png'}, stream=httpx.ByteStream(b'png'))
    mock_web(monkeypatch, serve)
    result = asyncio.run(web.fetch(current, 'https://example.org/start'))
    assert seen == ['https://example.org/start', 'https://example.org/image.png']
    assert result['url'] == seen[-1]
    mock_web(monkeypatch, lambda req: httpx.Response(403))
    with pytest.raises(ValueError, match='HTTP 403'):
        asyncio.run(web.fetch(current, 'https://example.org/blocked'))


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'http://127.0.0.1/', 'http://[::1]/',
                                'http://169.254.169.254/', 'https://user:password@example.org/',
                                'http://[::ffff:127.0.0.1]/', 'http://[64:ff9b::7f00:1]/'])
def test_bad_urls_rejected_before_request(tmp_path, monkeypatch, url):
    current, _ = job(tmp_path)
    def never(req): raise AssertionError('Unexpected network request')
    mock_web(monkeypatch, never)
    with pytest.raises(ValueError, match='public HTTP'):
        asyncio.run(web.fetch(current, url))


def test_private_redirect_rejected_and_redirect_loop_bounded(tmp_path, monkeypatch):
    current, _ = job(tmp_path)
    seen = []
    def redirect(req):
        seen.append(req)
        return httpx.Response(302, headers={'location': 'http://127.0.0.1/private'})
    mock_web(monkeypatch, redirect)
    with pytest.raises(ValueError): asyncio.run(web.fetch(current, 'https://example.org/'))
    assert len(seen) == 1
    seen.clear()
    def loop(req):
        seen.append(req)
        return httpx.Response(302, headers={'location': '/loop'})
    mock_web(monkeypatch, loop)
    with pytest.raises(ValueError, match='redirect limit'):
        asyncio.run(web.fetch(current, 'https://example.org/'))
    assert len(seen) == 6


class Chunks(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'x' * 40
        yield b'y' * 40


@pytest.mark.parametrize('declared', [False, True])
def test_download_size_limit_and_partial_cleanup(tmp_path, monkeypatch, declared):
    current, _ = job(tmp_path, file_bytes=50)
    headers = {'content-length': '80'} if declared else {}
    mock_web(monkeypatch, lambda req: httpx.Response(200, headers=headers, stream=Chunks()))
    with pytest.raises(ValueError, match='size limit'):
        asyncio.run(web.fetch(current, 'https://example.org/large'))
    assert not list(current.work.rglob('*.*'))


def test_workspace_limit_and_compression_rejected(tmp_path, monkeypatch):
    current, _ = job(tmp_path, workspace_bytes=10)
    current.write_file('existing.txt', '12345678')
    mock_web(monkeypatch, lambda req: httpx.Response(200, stream=httpx.ByteStream(b'abcde')))
    with pytest.raises(ValueError, match='storage limit'):
        asyncio.run(web.fetch(current, 'https://example.org/new'))
    assert [p.name for p in current.work.rglob('*') if p.is_file()] == ['existing.txt']
    mock_web(monkeypatch, lambda req: httpx.Response(200, headers={'content-encoding': 'gzip'}, stream=Chunks()))
    with pytest.raises(ValueError, match='uncompressed'):
        asyncio.run(web.fetch(current, 'https://example.org/compressed'))


def test_cancellation_removes_partial_download(tmp_path, monkeypatch):
    current, _ = job(tmp_path)
    async def scenario():
        started = asyncio.Event()
        class Hanging(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'x' * 65536
                started.set()
                await asyncio.Event().wait()
        mock_web(monkeypatch, lambda req: httpx.Response(200, stream=Hanging()))
        task = asyncio.create_task(web.fetch(current, 'https://example.org/slow'))
        await started.wait()
        assert any(p.is_file() for p in current.work.rglob('*'))
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert not any(p.is_file() for p in current.work.rglob('*'))
    asyncio.run(scenario())


def records(*ips):
    return [(socket.AF_INET6 if ':' in ip else socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443)) for ip in ips]


def test_backend_pins_dns_and_preserves_tls_hostname(monkeypatch):
    async def scenario():
        backend = web.PublicBackend()
        resolutions, connections, tls_names = [], [], []
        async def resolve(host, port, **kwargs):
            resolutions.append(host)
            return records('93.184.216.34') if len(resolutions) == 1 else records('127.0.0.1')
        monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
        class Stream(httpcore.AsyncMockStream):
            async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
                tls_names.append(server_hostname)
                return self
        class Connector:
            async def connect_tcp(self, host, port, **kwargs):
                connections.append(host)
                return Stream([b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK'])
        backend.backend = Connector()
        transport = web.PublicTransport()
        await transport.pool.aclose()
        transport.pool = httpcore.AsyncConnectionPool(network_backend=backend, ssl_context=web.ssl.create_default_context())
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            result = await client.get('https://example.org/image')
            assert result.text == 'OK'
        assert resolutions == ['example.org']
        assert connections == ['93.184.216.34']
        assert tls_names == ['example.org']
    asyncio.run(scenario())


@pytest.mark.parametrize('ips', [('127.0.0.1',), ('10.0.0.1',), ('169.254.169.254',),
                               ('93.184.216.34', '192.168.1.1'), ('::1',), ('100.100.100.100',)])
def test_backend_denies_nonpublic_dns_answers(monkeypatch, ips):
    async def scenario():
        async def resolve(*args, **kwargs): return records(*ips)
        monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
        backend = web.PublicBackend()
        with pytest.raises(ValueError, match='public web'):
            await backend.connect_tcp('example.org', 443, timeout=1)
    asyncio.run(scenario())


def test_redirect_hostname_is_checked_at_connection(tmp_path, monkeypatch):
    current, _ = job(tmp_path)
    async def scenario():
        connected = []
        async def resolve(host, port, **kwargs):
            return records('93.184.216.34' if host == 'example.org' else '127.0.0.1')
        monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
        async def connect(self, host, port, **kwargs):
            connected.append(host)
            return httpcore.AsyncMockStream([b'HTTP/1.1 302 Found\r\nLocation: http://internal.example/secret\r\nContent-Length: 0\r\n\r\n'])
        monkeypatch.setattr(httpcore.AnyIOBackend, 'connect_tcp', connect)
        with pytest.raises(ValueError, match='public web'):
            await web.fetch(current, 'http://example.org/start')
        assert connected == ['93.184.216.34']
    asyncio.run(scenario())
