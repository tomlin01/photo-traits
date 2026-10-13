"""Bounded HTTPS reads for approved attachment origins; no redirects or proxies.

Descriptor values are short-lived input, never log messages, identifiers on disk,
or proof of ownership. DNS is validated and the selected address is pinned while
TLS still verifies the original hostname. Tests inject both DNS and transport.
"""
import http.client
import ipaddress
import queue
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

MAX_BYTES = 1_000_000
FILE_SCHEMA = {'type': 'object', 'properties': {
    'download_url': {'type': 'string', 'maxLength': 8192},
    'file_id': {'type': 'string', 'maxLength': 256},
    'mime_type': {'type': 'string', 'maxLength': 100},
    'file_name': {'type': 'string', 'maxLength': 256}},
    'required': ['download_url', 'file_id'], 'additionalProperties': False}


class DownloadError(ValueError):
    pass


def reject():
    raise DownloadError('Attachment unavailable or unsafe. Reattach one JPEG; no automatic retry.')


def descriptor(value):
    if (not isinstance(value, dict) or not {'download_url', 'file_id'} <= value.keys()
            or value.keys() - FILE_SCHEMA['properties'].keys()):
        reject()
    for name, data in value.items():
        if (not isinstance(data, str) or not data or len(data) > FILE_SCHEMA['properties'][name]['maxLength']
                or any(ord(c) < 32 or ord(c) == 127 for c in data)):
            reject()
    if value.get('mime_type', 'image/jpeg') != 'image/jpeg':
        reject()
    return value['download_url']


def host_name(host):
    if (not isinstance(host, str) or len(host) > 253 or host != host.lower()
            or not re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)+', host)
            or any(len(part) > 63 or part.startswith('-') or part.endswith('-') for part in host.split('.'))):
        reject()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    reject()


def target(url, allowed_hosts):
    try:
        if (not isinstance(url, str) or not 1 <= len(url) <= 8192 or not url.isascii()
                or any(ord(c) <= 32 or ord(c) >= 127 for c in url) or '\\' in url):
            reject()
        parts = urlsplit(url)
        host = host_name(parts.hostname)
        if (parts.scheme != 'https' or parts.username is not None or parts.password is not None
                or parts.fragment or parts.port not in (None, 443) or host not in allowed_hosts
                or parts.netloc not in (host, host + ':443')):
            reject()
        return host, (parts.path or '/') + ('?' + parts.query if parts.query else '')
    except (ValueError, TypeError, AttributeError):
        reject()


def resolve_public(host, timeout):
    """Bound DNS waiting; abandoned DNS never initiates a subsequent connection."""
    result = queue.Queue(maxsize=1)
    def lookup():
        try:
            result.put(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
        except Exception:
            result.put(None)
    threading.Thread(target=lookup, daemon=True).start()
    try:
        rows = result.get(timeout=timeout)
    except queue.Empty:
        reject()
    if not rows:
        reject()
    return [row[4][0] for row in rows]


def public_addresses(addresses):
    if not addresses or len(addresses) > 32:
        reject()
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
            if (not ip.is_global or '%' in address or ip.is_multicast
                    or (ip.version == 6 and (ip.ipv4_mapped or ip.sixtofour or ip.teredo))):
                reject()
        except ValueError:
            reject()
    return addresses


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout, context):
        super().__init__(host, port=443, timeout=timeout, context=context)
        self.address = address

    def connect(self):
        sock = socket.socket(socket.AF_INET6 if ':' in self.address else socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect((self.address, 443))
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


class SafeHTTPS:
    def __init__(self, hosts=(), *, resolver=resolve_public, connection=PinnedHTTPS,
                 clock=time.monotonic, context=None, timeout=15):
        self.hosts = frozenset(host_name(host) for host in hosts)
        self.resolver, self.connection, self.clock = resolver, connection, clock
        self.context = context or ssl.create_default_context()
        # A caller cannot inject an unverified TLS policy into the live reader.
        if not self.context.check_hostname or self.context.verify_mode != ssl.CERT_REQUIRED:
            reject()
        self.timeout = timeout

    def read(self, url, *, limit, mime_types, cancelled=None):
        conn = None
        deadline = self.clock() + self.timeout
        def remaining():
            value = deadline - self.clock()
            if value <= 0 or (cancelled is not None and cancelled.is_set()):
                reject()
            return value
        try:
            host, path = target(url, self.hosts)
            addresses = public_addresses(self.resolver(host, remaining()))
            conn = self.connection(host, addresses[0], remaining(), self.context)
            conn.request('GET', path, headers={'Accept': ', '.join(mime_types), 'Accept-Encoding': 'identity'})
            stream_socket = conn.sock  # HTTPConnection may detach it on Connection: close.
            response = conn.getresponse()
            remaining()
            headers = response.headers
            lengths = headers.get_all('Content-Length', [])
            transfers = headers.get_all('Transfer-Encoding', [])
            types = headers.get_all('Content-Type', [])
            encodings = headers.get_all('Content-Encoding', [])
            if (response.status != 200 or len(types) != 1 or types[0].split(';', 1)[0].strip().lower() not in mime_types
                    or encodings not in ([], ['identity']) or len(lengths) > 1
                    or transfers not in ([], ['chunked']) or (lengths and transfers)):
                reject()
            length = None
            if lengths:
                if not re.fullmatch(r'[0-9]{1,9}', lengths[0]):
                    reject()
                length = int(lengths[0])
                if not 0 < length <= limit:
                    reject()
            data = bytearray()
            while True:
                stream_socket.settimeout(remaining())
                chunk = response.read1(min(65536, limit + 1 - len(data)))
                remaining()
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > limit:
                    reject()
            if not data or (length is not None and len(data) != length):
                reject()
            return bytes(data)
        except Exception:
            reject()  # Never reflect URL, response text, DNS error or signed query.
        finally:
            if conn is not None:
                conn.close()

    def download(self, value, cancelled):
        return self.read(descriptor(value), limit=MAX_BYTES, mime_types=('image/jpeg',), cancelled=cancelled)
