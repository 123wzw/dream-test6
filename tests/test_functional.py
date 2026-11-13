"""服务器功能回归测试（裸 socket）。"""

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


def _request(port: int, payload: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(payload)
        chunks = []
        while True:
            data = sock.recv(4096)
            if not data:
                break
            chunks.append(data)
    return b"".join(chunks)


def test_get_root(server):
    resp = _request(server, b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
    assert resp.startswith(b"HTTP/1.1 200")
    assert resp.endswith(b"OK")


def test_post_echo_with_content_length(server):
    body = b"hello world"
    payload = (
        b"POST /echo HTTP/1.1\r\nHost: localhost\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    )
    resp = _request(server, payload)
    assert resp.startswith(b"HTTP/1.1 200")
    assert resp.endswith(body)


def test_unknown_path_returns_404(server):
    resp = _request(server, b"GET /missing HTTP/1.1\r\nHost: localhost\r\n\r\n")
    assert resp.startswith(b"HTTP/1.1 404")


def test_response_has_content_length(server):
    resp = _request(server, b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
    assert b"Content-Length:" in resp.split(b"\r\n\r\n")[0]
