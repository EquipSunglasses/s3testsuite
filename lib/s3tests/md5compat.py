"""Pure-Python MD5 (RFC 1321), used only when hashlib refuses MD5.

On FIPS-enabled hosts whose Python lacks hashlib's usedforsecurity flag
(possible on RHEL 7), hashlib.md5() raises. S3 uses MD5 as an integrity
checksum (ETag, Content-MD5), not for security, so this fallback keeps the
suite working there. It is slower than hashlib (roughly 1-3 MB/s).
"""

import struct

_S = [7, 12, 17, 22] * 4 + [5, 9, 14, 20] * 4 + [4, 11, 16, 23] * 4 + [6, 10, 15, 21] * 4
_K = [int(abs(__import__('math').sin(i + 1)) * 2 ** 32) & 0xffffffff for i in range(64)]
_M = 0xffffffff


def _rotl(x, c):
    return ((x << c) | (x >> (32 - c))) & _M


class PyMD5(object):
    digest_size = 16
    block_size = 64
    name = 'md5'

    def __init__(self, data=b''):
        self._h = [0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476]
        self._buf = b''
        self._len = 0
        if data:
            self.update(data)

    def _block(self, chunk):
        x = struct.unpack('<16I', chunk)
        a, b, c, d = self._h
        for i in range(64):
            if i < 16:
                f, g = (b & c) | (~b & d), i
            elif i < 32:
                f, g = (d & b) | (~d & c), (5 * i + 1) % 16
            elif i < 48:
                f, g = b ^ c ^ d, (3 * i + 5) % 16
            else:
                f, g = c ^ (b | (~d & _M)), (7 * i) % 16
            f = (f + a + _K[i] + x[g]) & _M
            a, d, c = d, c, b
            b = (b + _rotl(f, _S[i])) & _M
        self._h = [(v + n) & _M for v, n in zip(self._h, (a, b, c, d))]

    def update(self, data):
        data = bytes(data)
        self._len += len(data)
        buf = self._buf + data
        n = len(buf) - len(buf) % 64
        for i in range(0, n, 64):
            self._block(buf[i:i + 64])
        self._buf = buf[n:]

    def copy(self):
        c = PyMD5()
        c._h, c._buf, c._len = list(self._h), self._buf, self._len
        return c

    def digest(self):
        c = self.copy()
        bits = (c._len * 8) & 0xffffffffffffffff
        pad = b'\x80' + b'\x00' * ((55 - c._len) % 64) + struct.pack('<Q', bits)
        c.update(pad)
        return struct.pack('<4I', *c._h)

    def hexdigest(self):
        return ''.join('%02x' % b for b in bytearray(self.digest()))
