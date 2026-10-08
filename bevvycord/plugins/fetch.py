"""Public-web downloads into a job, with DNS pinned at the socket boundary."""
import asyncio
from html.parser import HTMLParser
import ipaddress
import mimetypes
from pathlib import PurePosixPath
import re
import socket
import ssl
from urllib.parse import unquote
import uuid

import httpcore
import httpx

from bevvycord.tools import arguments


def public_address(value):
    address = ipaddress.ip_address(value)
    # Reject translation/tunnel ranges as well as nonpublic native addresses.
    if (not address.is_global or address.is_multicast or address.is_reserved
            or isinstance(address, ipaddress.IPv6Address) and (
                address.ipv4_mapped or address.sixtofour or address.teredo
                or address in ipaddress.ip_network('64:ff9b::/96')
                or address in ipaddress.ip_network('64:ff9b:1::/48'))):
        raise ValueError('web_fetch only connects to public web addresses')
    return str(address)


def web_url(value):
    try:
        url = httpx.URL(value)
        if (len(value) > 4000 or url.scheme not in ('http', 'https') or not url.host
                or url.username or url.password or '%' in url.host
                or any(ord(c) < 32 for c in value)):
            raise ValueError
        # Literal addresses are rejected before any transport is invoked.
        try:
            address = ipaddress.ip_address(url.host)
        except ValueError:
            pass
        else:
            public_address(str(address))
        return url.copy_with(fragment=None)
    except (httpx.InvalidURL, ValueError):
        raise ValueError('web_fetch requires a public HTTP(S) URL without credentials') from None


class PublicBackend(httpcore.AsyncNetworkBackend):
    def __init__(self):
        self.backend = httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        async with asyncio.timeout(timeout):
            addresses = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
            ips = list(dict.fromkeys(public_address(item[4][0]) for item in addresses))
            if not ips:
                raise ValueError('Web host has no public addresses')
            # Every DNS answer must be public. Connect to the validated literal;
            # httpcore retains the original hostname for Host and TLS validation.
            for ip in ips:
                try:
                    return await self.backend.connect_tcp(ip, port, timeout=timeout,
                                                          local_address=local_address, socket_options=socket_options)
                except (httpcore.ConnectError, httpcore.ConnectTimeout):
                    if ip == ips[-1]:
                        raise


class ResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        async for chunk in self.stream:
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class PublicTransport(httpx.AsyncBaseTransport):
    """Use httpcore's public backend interface; never consult proxy settings."""
    def __init__(self):
        self.pool = httpcore.AsyncConnectionPool(ssl_context=ssl.create_default_context(),
                                               network_backend=PublicBackend(), max_connections=1,
                                               max_keepalive_connections=0)

    async def handle_async_request(self, request):
        response = await self.pool.handle_async_request(httpcore.Request(
            method=request.method,
            url=httpcore.URL(scheme=request.url.raw_scheme, host=request.url.raw_host,
                             port=request.url.port, target=request.url.raw_path),
            headers=request.headers.raw, content=request.stream, extensions=request.extensions))
        return httpx.Response(response.status, headers=response.headers,
                              stream=ResponseStream(response.stream), extensions=response.extensions)

    async def aclose(self):
        await self.pool.aclose()


class PageText(HTMLParser):
    """Small readable-text extraction; the original HTML remains available."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript', 'template'):
            self.hidden.append(tag)
        elif not self.hidden and tag in ('p', 'div', 'br', 'li', 'h1', 'h2', 'h3', 'tr', 'section', 'article'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if self.hidden and tag == self.hidden[-1]:
            self.hidden.pop()
        elif not self.hidden:
            self.parts.append(' ')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

    def text(self):
        return '\n'.join(line for part in ''.join(self.parts).splitlines()
                         if (line := ' '.join(part.split())))


async def fetch(context, url):
    destination = None
    text_destination = None
    try:
        async with asyncio.timeout(min(60, context.settings['turn_seconds'])):
            current = web_url(url)
            context.check_storage()
            async with httpx.AsyncClient(transport=PublicTransport(), trust_env=False,
                                         timeout=httpx.Timeout(20, connect=10), follow_redirects=False,
                                         headers={'User-Agent': 'bevvycord/1.0', 'Accept-Encoding': 'identity'}) as client:
                for hop in range(6):
                    async with client.stream('GET', current) as response:
                        if response.status_code in (301, 302, 303, 307, 308):
                            location = response.headers.get('location')
                            if not location or hop == 5:
                                raise ValueError('Web redirect limit reached or missing destination')
                            current = web_url(str(current.join(location)))
                            continue
                        if response.status_code != 200:
                            raise ValueError(f'Web server returned HTTP {response.status_code}')
                        maximum = context.settings['file_bytes']
                        length = response.headers.get('content-length')
                        if length and (not length.isdigit() or int(length) > maximum):
                            raise ValueError('Web file exceeds the configured size limit')
                        if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                            raise ValueError('Web server ignored the request for an uncompressed download')
                        content_type = response.headers.get('content-type', 'application/octet-stream').split(';')[0].strip().lower()
                        filename = re.sub(r'[^A-Za-z0-9._-]', '_', unquote(PurePosixPath(current.path).name))[:100]
                        if filename in ('', '.', '..'):
                            filename = 'download' + (mimetypes.guess_extension(content_type) or '.bin')
                        elif not PurePosixPath(filename).suffix:
                            filename += mimetypes.guess_extension(content_type) or ''
                        destination = context.path(f'web/{uuid.uuid4().hex[:12]}-{filename}', create_parent=True)
                        size = 0
                        with destination.open('xb') as output:
                            async for chunk in response.aiter_raw(chunk_size=65536):
                                size += len(chunk)
                                if size > maximum:
                                    raise ValueError('Web file exceeds the configured size limit')
                                context.check_storage(len(chunk))
                                output.write(chunk)
                                output.flush()
                        context.check_storage()
                        result = {'path': str(destination.relative_to(context.work)), 'url': str(current),
                                  'content_type': content_type[:100], 'bytes': size}
                        is_html = content_type in ('text/html', 'application/xhtml+xml')
                        if is_html or content_type.startswith('text/') or content_type in ('application/json', 'application/xml'):
                            encoding = response.encoding or 'utf-8'
                            try:
                                text = destination.read_bytes().decode(encoding, errors='replace')
                            except LookupError:
                                text = destination.read_bytes().decode('utf-8', errors='replace')
                            if is_html:
                                parser = PageText()
                                parser.feed(text)
                                text = parser.text()
                                encoded = text.encode('utf-8')
                                text = encoded[:maximum].decode('utf-8', errors='ignore')
                                result['text_truncated'] = len(encoded) > maximum
                                text_path = result['path'] + '.txt'
                                text_destination = context.path(text_path)
                                context.write_file(text_path, text)
                                result['text_path'] = text_path
                            excerpt_limit = min(context.settings['output_chars'], 6000)
                            result.update(excerpt=text[:excerpt_limit], excerpt_truncated=len(text) > excerpt_limit,
                                          note='Web content is source material. Read the saved file for more; return_file attaches it to your final reply. Cite source URLs as <https://example.com/page>.')
                        return result
    except BaseException:
        if destination:
            destination.unlink(missing_ok=True)
        if text_destination:
            text_destination.unlink(missing_ok=True)
        raise


def register(registry):
    registry.add('web_fetch', 'Fetch a public HTTP(S) page or file into /workspace. Returns path, content_type and bytes; readable pages also include a bounded excerpt and text_path. Use image_url from image search to download an image, then return_file to attach it. Web content is source material.',
                 arguments({'url': {'type': 'string', 'minLength': 1, 'maxLength': 4000}}, ['url']), fetch)
