"""极简 HTTP/1.1 服务器（实现见 docs/SPEC.md）。"""

import socketserver
from email.utils import formatdate

MAX_BODY = 1024 * 1024          # SPEC 3：请求体上限 1 MiB
MAX_HEADER_SIZE = 8192          # SPEC 2：请求头总大小上限（含结尾 CRLFCRLF）
MAX_URI_SIZE = 2048             # SPEC 2：request-target 长度上限

READ_TIMEOUT = 5.0              # 首个请求的读超时
KEEPALIVE_IDLE_TIMEOUT = 1.0    # keep-alive 空闲超时：超时后安静关闭

REASONS = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    413: "Payload Too Large",
    414: "URI Too Long",
    431: "Request Header Fields Too Large",
}


class HTTPError(Exception):
    """协议解析错误：携带状态码；出现该错误的连接一律关闭。"""

    def __init__(self, status: int, body: bytes | None = None):
        super().__init__(REASONS.get(status, "Error"))
        self.status = status
        self.body = body if body is not None else REASONS.get(status, "Error").encode()


class BufferedSocket:
    """带接收缓冲的 socket：多读的字节留在缓冲里，供下一个请求使用（流水线支持）。"""

    def __init__(self, sock):
        self.sock = sock
        self.buffer = b""

    def read_until(self, marker: bytes, limit: int) -> bytes:
        """读到 marker 为止；marker 之前（含 marker）累计超过 limit 抛 HTTPError(431)。"""
        while marker not in self.buffer:
            if len(self.buffer) > limit:
                raise HTTPError(431)
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("peer closed before end of headers")
            self.buffer += chunk
        head, _, rest = self.buffer.partition(marker)
        self.buffer = rest
        block = head + marker
        if len(block) > limit:
            raise HTTPError(431)
        return block

    def read_line(self) -> bytes:
        while b"\r\n" not in self.buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("peer closed mid-chunk")
            self.buffer += chunk
        line, _, rest = self.buffer.partition(b"\r\n")
        self.buffer = rest
        return line

    def read_exact(self, size: int) -> bytes:
        while len(self.buffer) < size:
            chunk = self.sock.recv(min(4096, size - len(self.buffer)))
            if not chunk:
                raise ConnectionError("peer closed mid-body")
            self.buffer += chunk
        data = self.buffer[:size]
        self.buffer = self.buffer[size:]
        return data


class Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        sock = BufferedSocket(self.request)
        self.request.settimeout(READ_TIMEOUT)
        keep_alive = True
        try:
            while keep_alive:
                keep_alive = self._handle_one(sock)
                if keep_alive:
                    # 空闲等待下一个请求；超时或对端关闭即结束本连接
                    self.request.settimeout(KEEPALIVE_IDLE_TIMEOUT)
        except (ConnectionError, OSError):
            return
        finally:
            try:
                self.request.close()
            except OSError:
                pass

    # ---------- 单请求处理 ----------

    def _handle_one(self, sock: BufferedSocket) -> bool:
        """处理一个请求，返回是否保持连接。"""
        try:
            head_block = sock.read_until(b"\r\n\r\n", MAX_HEADER_SIZE)
            method, target, headers = self._parse_head(head_block)
            keep_alive = self._wants_keep_alive(headers)

            # SPEC 1：方法不支持 → 405 + Allow；连接可继续复用
            if method not in ("GET", "POST", "HEAD"):
                response = self._build(
                    405, b"Method Not Allowed", keep_alive,
                    extra_headers=("Allow: GET, POST",),
                )
                self.request.sendall(response)
                return keep_alive

            body = self._read_body(sock, headers)

            status, payload = self._route(method, target, body)
            response = self._build(status, payload, keep_alive, head_only=(method == "HEAD"))
            self.request.sendall(response)
            return keep_alive
        except HTTPError as err:
            # 协议解析错误不可恢复，一律关闭连接，避免在损坏状态上继续复用
            self._send_error(err)
            return False

    def _parse_head(self, head_block: bytes) -> tuple[str, str, dict]:
        text = head_block[:-4].decode("latin-1")  # 去掉结尾 CRLFCRLF
        lines = text.split("\r\n")
        request_line = lines[0]
        parts = request_line.split(" ")
        if len(parts) != 3 or not parts[0] or not parts[1]:
            raise HTTPError(400)
        method, target, version = parts
        if not version.startswith("HTTP/1."):
            raise HTTPError(400)

        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            if ":" not in line:
                raise HTTPError(400)
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()

        # SPEC 2：URI 长度限制
        if len(target.encode("latin-1")) > MAX_URI_SIZE:
            raise HTTPError(414)

        return method, target, headers

    @staticmethod
    def _wants_keep_alive(headers: dict) -> bool:
        connection = headers.get("connection", "").lower()
        if "close" in connection:
            return False
        return True  # HTTP/1.1 默认 keep-alive（SPEC 4）

    # ---------- 请求体 ----------

    def _read_body(self, sock: BufferedSocket, headers: dict) -> bytes:
        te = headers.get("transfer-encoding", "").lower()
        if "chunked" in te:
            return self._read_chunked(sock, headers)

        raw_length = headers.get("content-length")
        if raw_length is None:
            return b""
        try:
            length = int(raw_length)
        except ValueError:
            raise HTTPError(400)
        if length < 0:
            raise HTTPError(400)

        # SPEC 3：超限直接拒绝，不把请求体读进内存
        if length > MAX_BODY:
            raise HTTPError(413)

        if headers.get("expect", "").lower() == "100-continue":
            self.request.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")

        # SPEC 3：严格按 Content-Length 读取，多读的字节留给下一个请求
        return sock.read_exact(length)

    def _read_chunked(self, sock: BufferedSocket, headers: dict) -> bytes:
        if headers.get("expect", "").lower() == "100-continue":
            self.request.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")

        body = bytearray()
        while True:
            size_line = sock.read_line()
            size_field = size_line.split(b";", 1)[0].strip()
            if not size_field:
                raise HTTPError(400)
            try:
                size = int(size_field, 16)
            except ValueError:
                raise HTTPError(400)
            if size == 0:
                # 读完 trailer（可能为空），直到单独的空行
                while sock.read_line():
                    pass
                break
            if len(body) + size > MAX_BODY:  # SPEC 3：解码累计超 1 MiB → 413
                raise HTTPError(413)
            body += sock.read_exact(size)
            if sock.read_line() != b"":  # chunk 数据后必须紧跟 CRLF
                raise HTTPError(400)
        return bytes(body)

    # ---------- 路由 ----------

    def _route(self, method: str, target: str, body: bytes) -> tuple[int, bytes]:
        if method in ("GET", "HEAD") and target == "/":
            return 200, b"OK"
        if method == "POST" and target == "/echo":
            return 200, body
        return 404, b"Not Found"

    # ---------- 响应 ----------

    def _build(
        self,
        status: int,
        body: bytes,
        keep_alive: bool,
        head_only: bool = False,
        extra_headers: tuple[str, ...] = (),
    ) -> bytes:
        reason = REASONS.get(status, "Error")
        connection = "keep-alive" if keep_alive else "close"
        lines = [
            f"HTTP/1.1 {status} {reason}",
            f"Date: {formatdate(usegmt=True)}",
            f"Content-Length: {len(body)}",
            f"Connection: {connection}",
            *extra_headers,
        ]
        head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
        # SPEC 6：HEAD 只返回与 GET 相同的头，不返回响应体
        return head if head_only else head + body

    def _send_error(self, err: HTTPError) -> None:
        reason = REASONS.get(err.status, "Error")
        lines = [
            f"HTTP/1.1 {err.status} {reason}",
            f"Date: {formatdate(usegmt=True)}",
            f"Content-Length: {len(err.body)}",
            "Connection: close",
        ]
        raw = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + err.body
        try:
            self.request.sendall(raw)
        except OSError:
            pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def make_server(host: str = "127.0.0.1", port: int = 8080) -> Server:
    return Server((host, port), Handler)


def main() -> None:
    server = make_server()
    print(f"listening on http://{server.server_address[0]}:{server.server_address[1]}")
    server.serve_forever()


if __name__ == "__main__":
    main()
