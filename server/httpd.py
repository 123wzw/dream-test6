"""极简 HTTP/1.1 服务器。"""

import socketserver

MAX_BODY = 1024 * 1024


class Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        sock = self.request
        sock.settimeout(5)
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = sock.recv(4096)
            if not chunk:
                return
            buffer += chunk

        head, _, rest = buffer.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, _version = lines[0].split(" ")
        except ValueError:
            sock.sendall(self._build(400, b"Bad Request"))
            return

        headers = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()

        length = int(headers.get("content-length", "0") or 0)
        body = rest
        while len(body) < length:
            chunk = sock.recv(4096)
            if not chunk:
                break
            body += chunk
        body = body[:length]

        sock.sendall(self._route(method, target, headers, body))

    def _route(self, method: str, target: str, headers: dict, body: bytes) -> bytes:
        if method == "GET" and target == "/":
            return self._build(200, b"OK")
        if method == "POST" and target == "/echo":
            return self._build(200, body)
        return self._build(404, b"Not Found")

    def _build(self, status: int, body: bytes) -> bytes:
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found"}.get(status, "Error")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("latin-1")
        return head + body


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
