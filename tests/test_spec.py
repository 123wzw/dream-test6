"""SPEC 验收测试：全部用裸 socket 逐条构造请求。

每个用例对应 docs/SPEC.md 的一条要求；在修复前的 httpd.py 上必须失败。
"""

import socket
import threading

import pytest

from server import httpd


@pytest.fixture()
def server():
    srv = httpd.make_server("127.0.0.1", 0)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield port
    srv.shutdown()
    srv.server_close()


class Conn:
    """带接收缓冲的裸 socket 连接，缓冲保证流水线/多余字节不被吞掉。"""

    def __init__(self, port: int):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.buf = b""

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def read_response(self, read_body: bool = True) -> tuple[bytes, bytes]:
        """读取一个完整响应，返回 (原始头块含末尾 CRLFCRLF, 响应体)。"""
        raw = self._read_until(b"\r\n\r\n")
        head, _, _ = raw.rpartition(b"\r\n\r\n")
        headers = self._parse_headers(raw)
        body = b""
        if read_body:
            length = int(headers.get(b"content-length", b"0") or 0)
            body = self._read_n(length)
        return raw, body

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self.buf:
            data = self.sock.recv(4096)
            if not data:
                result = self.buf
                self.buf = b""
                return result
            self.buf += data
        before, _, after = self.buf.partition(marker)
        self.buf = after
        return before + marker

    def _read_n(self, n: int) -> bytes:
        while len(self.buf) < n:
            data = self.sock.recv(4096)
            if not data:
                break
            self.buf += data
        result = self.buf[:n]
        self.buf = self.buf[n:]
        return result

    @staticmethod
    def _parse_headers(raw: bytes) -> dict[bytes, bytes]:
        head = raw.split(b"\r\n\r\n", 1)[0]
        lines = head.split(b"\r\n")
        result = {}
        for line in lines[1:]:
            if b":" in line:
                name, value = line.split(b":", 1)
                result[name.strip().lower()] = value.strip()
        return result

    def expect_closed(self) -> None:
        """服务器应当关闭连接：后续读到 EOF。"""
        try:
            while True:
                data = self.sock.recv(4096)
                if not data:
                    return
        except (ConnectionError, socket.timeout, OSError):
            return
        pytest.fail("expected server to close the connection")

    def close(self) -> None:
        self.sock.close()


# ---------- SPEC 1：请求行 / 方法 ----------

def test_unsupported_method_returns_405_with_allow(server):
    """SPEC 1：非 GET/POST 方法必须 405 且带 Allow: GET, POST。"""
    conn = Conn(server)
    conn.send(b"PUT / HTTP/1.1\r\nHost: localhost\r\n\r\n")
    raw, _ = conn.read_response()
    assert raw.startswith(b"HTTP/1.1 405")
    assert b"Allow: GET, POST" in raw
    conn.close()


# ---------- SPEC 2：请求头大小 / URI 长度 ----------

def test_header_oversize_returns_431_and_closes(server):
    """SPEC 2：请求头总大小 > 8192 字节 → 431 并关闭；恰好 8192 必须放行。"""
    limit = httpd.MAX_HEADER_SIZE

    # 边界：恰好等于上限的头块必须正常处理
    ok = Conn(server)
    base = b"GET / HTTP/1.1\r\nX-Pad: "
    suffix = b"\r\n\r\n"
    padding = limit - len(base) - len(suffix)
    ok_head = base + b"a" * padding + suffix
    assert len(ok_head) == limit
    ok.send(ok_head)
    raw, _ = ok.read_response()
    assert raw.startswith(b"HTTP/1.1 200")
    ok.close()

    # 超限：8193 字节 → 431，连接关闭
    bad = Conn(server)
    bad_head = ok_head[:-4] + b"a\r\n\r\n"
    assert len(bad_head) == limit + 1
    bad.send(bad_head)
    raw, _ = bad.read_response()
    assert raw.startswith(b"HTTP/1.1 431")
    bad.expect_closed()
    bad.close()


def test_uri_too_long_returns_414(server):
    """SPEC 2：request-target 长度 > 2048 字节 → 414；恰好 2048 必须放行。"""
    limit = httpd.MAX_URI_SIZE

    ok = Conn(server)
    target = b"/" + b"a" * (limit - 1)
    assert len(target) == limit
    ok.send(b"GET " + target + b" HTTP/1.1\r\nHost: localhost\r\n\r\n")
    raw, _ = ok.read_response()
    assert not raw.startswith(b"HTTP/1.1 414")
    ok.close()

    bad = Conn(server)
    long_target = b"/" + b"a" * limit
    assert len(long_target) == limit + 1
    bad.send(b"GET " + long_target + b" HTTP/1.1\r\nHost: localhost\r\n\r\n")
    raw, _ = bad.read_response()
    assert raw.startswith(b"HTTP/1.1 414")
    bad.close()


# ---------- SPEC 3：请求体 ----------

def test_chunked_encoding_is_decoded(server):
    """SPEC 3：Transfer-Encoding: chunked 必须按 chunked 规则解码，POST /echo 回显解码结果。"""
    conn = Conn(server)
    payload = (
        b"POST /echo HTTP/1.1\r\nHost: localhost\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n"
        b"5\r\nhello\r\n"
        b"7\r\n world!\r\n"
        b"0\r\n\r\n"
    )
    conn.send(payload)
    raw, body = conn.read_response()
    assert raw.startswith(b"HTTP/1.1 200")
    assert body == b"hello world!"
    conn.close()


def test_chunked_arriving_in_fragments_is_decoded(server):
    """SPEC 3：分块逐个字节到达也必须正确解码（不得依赖一次性 recv）。"""
    conn = Conn(server)
    conn.send(b"POST /echo HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n\r\n")
    for piece in (b"4\r\n", b"abcd", b"\r\n", b"2\r\nEF\r\n", b"0\r\n\r\n"):
        conn.send(piece)
    raw, body = conn.read_response()
    assert raw.startswith(b"HTTP/1.1 200")
    assert body == b"abcdEF"
    conn.close()


def test_body_oversize_content_length_returns_413(server):
    """SPEC 3：Content-Length 声明的请求体 > 1 MiB → 413。"""
    conn = Conn(server)
    too_big = httpd.MAX_BODY + 1
    conn.send(
        b"POST /echo HTTP/1.1\r\nHost: localhost\r\n"
        b"Content-Length: " + str(too_big).encode() + b"\r\n\r\n"
    )
    raw, _ = conn.read_response()
    assert raw.startswith(b"HTTP/1.1 413")
    conn.expect_closed()
    conn.close()


def test_body_oversize_chunked_returns_413(server):
    """SPEC 3：chunked 实际解码累计超过 1 MiB → 413，而不是把整个请求体吃进内存。"""
    conn = Conn(server)
    chunk = httpd.MAX_BODY // 2
    conn.send(
        b"POST /echo HTTP/1.1\r\nHost: localhost\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n"
    )
    conn.send(("%x\r\n" % chunk).encode() + b"x" * chunk + b"\r\n")
    conn.send(("%x\r\n" % chunk).encode() + b"y" * chunk + b"\r\n")
    conn.send(b"10\r\nzzzzzzzzzzzzzzzz\r\n0\r\n\r\n")
    raw, _ = conn.read_response()
    assert raw.startswith(b"HTTP/1.1 413")
    conn.expect_closed()
    conn.close()


def test_expect_100_continue_is_honored(server):
    """SPEC 3：Expect: 100-continue 时必须先回 100 Continue，再读请求体。"""
    conn = Conn(server)
    body = b"payload after continue"
    conn.send(
        b"POST /echo HTTP/1.1\r\nHost: localhost\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Expect: 100-continue\r\n\r\n"
    )
    interim = conn._read_until(b"\r\n\r\n")
    assert interim.startswith(b"HTTP/1.1 100")
    conn.send(body)
    raw, resp_body = conn.read_response()
    assert raw.startswith(b"HTTP/1.1 200")
    assert resp_body == body
    conn.close()


def test_content_length_does_not_overread_pipelined_request(server):
    """SPEC 3 + SPEC 6：按 Content-Length 精确读取，多出来的字节属于下一个请求。"""
    conn = Conn(server)
    first = (
        b"POST /echo HTTP/1.1\r\nHost: localhost\r\n"
        b"Content-Length: 5\r\n\r\nhello"
    )
    second = b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n"
    conn.send(first + second)
    raw1, body1 = conn.read_response()
    assert raw1.startswith(b"HTTP/1.1 200")
    assert body1 == b"hello"
    raw2, body2 = conn.read_response()
    assert raw2.startswith(b"HTTP/1.1 200")
    assert body2 == b"OK"
    conn.close()


# ---------- SPEC 4：连接管理 ----------

def test_keepalive_serves_two_requests_on_one_connection(server):
    """SPEC 4：HTTP/1.1 默认 keep-alive，同一 socket 两个请求都要正确响应。"""
    conn = Conn(server)
    conn.send(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
    raw1, body1 = conn.read_response()
    assert raw1.startswith(b"HTTP/1.1 200")
    assert body1 == b"OK"
    assert b"Connection: keep-alive" in raw1

    conn.send(b"GET /missing HTTP/1.1\r\nHost: localhost\r\n\r\n")
    raw2, _ = conn.read_response()
    assert raw2.startswith(b"HTTP/1.1 404")
    assert b"Connection: keep-alive" in raw2
    conn.close()


def test_connection_close_header_closes_socket(server):
    """SPEC 4：Connection: close 时响应带 Connection: close 并真正关闭连接。"""
    conn = Conn(server)
    conn.send(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
    raw, body = conn.read_response()
    assert raw.startswith(b"HTTP/1.1 200")
    assert body == b"OK"
    assert b"Connection: close" in raw
    conn.expect_closed()
    conn.close()


# ---------- SPEC 5：响应头 ----------

def test_every_response_has_rfc1123_date(server):
    """SPEC 5：每个响应必须带 RFC 1123 格式的 Date 头。"""
    from email.utils import parsedate_to_datetime

    conn = Conn(server)
    conn.send(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
    raw, _ = conn.read_response()
    assert b"Date:" in raw
    date_line = next(line for line in raw.split(b"\r\n") if line.lower().startswith(b"date:"))
    value = date_line.split(b":", 1)[1].strip().decode("latin-1")
    parsed = parsedate_to_datetime(value)  # 格式非法会抛异常
    assert parsed is not None
    conn.close()


# ---------- SPEC 6：HEAD 与流水线 ----------

def test_head_has_headers_but_no_body(server):
    """SPEC 6：HEAD 与 GET 同状态行/响应头（含相同 Content-Length），但无响应体。"""
    get_conn = Conn(server)
    get_conn.send(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
    get_raw, get_body = get_conn.read_response()
    get_conn.close()
    get_headers = get_raw.split(b"\r\n\r\n", 1)[0]
    get_cl = next(
        line for line in get_headers.split(b"\r\n") if line.lower().startswith(b"content-length:")
    )

    conn = Conn(server)
    conn.send(b"HEAD / HTTP/1.1\r\nHost: localhost\r\n\r\n")
    # HEAD 无响应体：只读到响应头为止，不能按 Content-Length 等一个不存在的 body
    raw, body = conn.read_response(read_body=False)
    assert raw.startswith(b"HTTP/1.1 200")
    assert get_cl in raw
    assert body == b""
    # HEAD 之后连接仍可复用
    conn.send(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
    raw2, body2 = conn.read_response()
    assert raw2.startswith(b"HTTP/1.1 200")
    assert body2 == b"OK"
    conn.close()


def test_pipelining_responses_stay_in_order(server):
    """SPEC 6：流水线请求必须按发送顺序逐个返回，不得串包或丢弃。"""
    conn = Conn(server)
    requests = [
        b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n",
        b"GET /missing HTTP/1.1\r\nHost: localhost\r\n\r\n",
        b"POST /echo HTTP/1.1\r\nHost: localhost\r\nContent-Length: 3\r\n\r\nabc",
        b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n",
    ]
    for req in requests:
        conn.send(req)
    expected = [(200, b"OK"), (404, b"Not Found"), (200, b"abc"), (200, b"OK")]
    for status, body in expected:
        raw, got = conn.read_response()
        assert raw.startswith(("HTTP/1.1 %d" % status).encode())
        assert got == body
    conn.close()
