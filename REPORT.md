# mini-http 公网上线前 SPEC 自查报告

- 验收依据：`docs/SPEC.md`（未修改）
- 被测实现：`server/httpd.py`
- 复现测试：`tests/test_spec.py`（全部裸 socket，修复前 13 红 1 绿，修复后全绿）
- 回归测试：`tests/test_functional.py`（断言未修改，4 个用例始终全绿）
- 依赖：仅标准库（`socketserver`、`email.utils`），未引入第三方包
- 运行配置：监听地址/端口默认值 `127.0.0.1:8080` 未改动（`make_server` 签名与默认值保持原样）

## 结论汇总

| # | SPEC 条款 | 缺陷 | 修复后状态码 | 复现用例 |
|---|---|---|---|---|
| 1 | §2 请求头 | 请求头无大小上限，可无限累积内存 | 431 | `test_header_oversize_returns_431_and_closes` |
| 2 | §2 请求头 | request-target 无长度限制 | 414 | `test_uri_too_long_returns_414` |
| 3 | §1 请求行 | 非 GET/POST 方法未返回 405 + Allow | 405 | `test_unsupported_method_returns_405_with_allow` |
| 4 | §3 请求体 | 不解析 `Transfer-Encoding: chunked`，且过度读取造成请求走私 | 200（正确解码） | `test_chunked_encoding_is_decoded`、`test_chunked_arriving_in_fragments_is_decoded`、`test_content_length_does_not_overread_pipelined_request` |
| 5 | §3 请求体 | 请求体无 1 MiB 上限（Content-Length 与 chunked 两条路径均无） | 413 | `test_body_oversize_content_length_returns_413`、`test_body_oversize_chunked_returns_413` |
| 6 | §3 请求体 | 不支持 `Expect: 100-continue` | 100 + 200 | `test_expect_100_continue_is_honored` |
| 7 | §4 连接管理 | 每个响应固定 `Connection: close` 且处理完一个请求即退出，不支持 keep-alive | keep-alive/close 与实际行为一致 | `test_keepalive_serves_two_requests_on_one_connection`、`test_connection_close_header_closes_socket` |
| 8 | §6 流水线 | 不支持流水线；`recv` 多读的字节随连接关闭被丢弃，串包/丢包 | 按序响应 | `test_pipelining_responses_stay_order`、`test_content_length_does_not_overread_pipelined_request` |
| 9 | §5 响应头 | 响应没有 `Date` 头 | 带 RFC 1123 Date | `test_every_response_has_rfc1123_date` |
| 10 | §6 特殊方法 | 不支持 HEAD（落到 404 且带响应体） | HEAD 无头外响应体 | `test_head_has_headers_but_no_body` |

---

## 逐条详情

### 1. 请求头无大小上限 —— 内存耗尽（SPEC §2）

- **代码位置（修复前）**：`server/httpd.py:12-17`，`handle()` 中
  `while b"\r\n\r\n" not in buffer: ... buffer += chunk`，没有任何累计上限。
- **当前行为**：连接持续发送不含 `\r\n\r\n` 的数据，`buffer` 无限增长，直到内存耗尽；公网上可被单连接低成本打满。
- **正确行为**：请求头总大小超过 8192 字节立即返回 `431 Request Header Fields Too Large` 并关闭连接；恰好 8192 字节必须放行。
- **修复方式**：新增常量 `MAX_HEADER_SIZE = 8192`（`httpd.py:7`）；新增 `BufferedSocket.read_until(marker, limit)`（`httpd.py:47`），在读到 `\r\n\r\n` 的过程中逐次检查累计长度，超限即抛 `HTTPError(431)`，由 `_send_error()`（`httpd.py:250`）回 431 并关闭连接。边界按“块总长度（含结尾 `\r\n\r\n`）> 8192 即超限”实现，与 SPEC 字面一致。
- **测试用例**：`test_header_oversize_returns_431_and_closes`（同时验证 8192 字节放行、8193 字节 431 且连接被关）。

### 2. URI 长度不限制（SPEC §2）

- **代码位置（修复前）**：`server/httpd.py:22`，请求行 `split(" ")` 后直接使用 target，无长度校验。
- **当前行为**：任意长度的 request-target 都被接受，配合 §1 的无头限制可放大内存占用；扫描报告将其列为可利用项。
- **正确行为**：request-target 长度超过 2048 字节返回 `414 URI Too Long`；恰好 2048 字节放行。
- **修复方式**：新增常量 `MAX_URI_SIZE = 2048`（`httpd.py:8`）；在 `_parse_head()`（`httpd.py:153`）中按 latin-1 字节长度校验，超限抛 `HTTPError(414)`。
- **测试用例**：`test_uri_too_long_returns_414`（覆盖 2048 放行、2049 返回 414 两个边界）。

### 3. 非 GET/POST 方法未返回 405（SPEC §1、§7）

- **代码位置（修复前）**：`server/httpd.py:44-49`，`_route()` 只识别 GET/POST，其余方法全部落到 `404 Not Found`；`_build()`（`httpd.py:51-59`）的 reason 表也没有 405。
- **当前行为**：`PUT / HTTP/1.1` 返回 404，没有 `Allow` 头。
- **正确行为**：非 GET/POST 方法返回 `405 Method Not Allowed`，并带 `Allow: GET, POST`。
- **修复方式**：`_handle_one()`（`httpd.py:112-118`）在解析后显式拦截白名单外方法，走 `_build(..., extra_headers=("Allow: GET, POST",))`（`httpd.py:229`）。405 属于语义错误而非协议解析失败，连接仍按客户端 `Connection` 头决定是否复用（已手动验证 405 后同连接可继续请求）。HEAD 按 SPEC §6 另行放行，不在 405 之列。
- **测试用例**：`test_unsupported_method_returns_405_with_allow`。

### 4. 不解析 chunked，且过度读取造成请求走私（SPEC §3、§6、§8）

- **代码位置（修复前）**：`server/httpd.py:33-40`。服务器只认 `Content-Length`：
  - 带 `Transfer-Encoding: chunked` 时 `length` 取默认值 0，请求体被整体忽略，`POST /echo` 回空；
  - `while len(body) < length` 用 `recv(4096)` 拼缓冲，最后 `body = body[:length]` 截断——多读出的字节（同一连接上的下一个请求）既不保留也不归还，连接随即关闭，构成典型的请求走私/流水线串包面。
- **当前行为**：chunked 请求无法工作；前置请求多读的后续请求字节被吞掉。
- **正确行为**：按 RFC 7230 chunked 规则解码（含 chunk 扩展 `;`、trailer 段、终止块 `0`），解码结果与 Content-Length 语义等价；`POST /echo` 回显解码后的内容；严格按声明长度读取，多读字节留给下一个请求。
- **修复方式**：
  - 新增 `BufferedSocket`（`httpd.py:33`）承载接收缓冲，`read_line()`/`read_exact()` 保证多读字节留在缓冲内；
  - 新增 `_read_chunked()`（`httpd.py:192`）：逐块读 size 行（剥离 chunk 扩展）、按 size 精确读数据并校验尾随 CRLF、读到 `0` 后吞掉 trailer；
  - `_read_body()`（`httpd.py:167`）优先识别 `Transfer-Encoding` 中的 `chunked`，否则按 Content-Length 用 `read_exact(length)` 精确读取，从机制上杜绝过度读取。
- **测试用例**：`test_chunked_encoding_is_decoded`、`test_chunked_arriving_in_fragments_is_decoded`（分片到达）、`test_content_length_does_not_overread_pipelined_request`（多读字节归属下一请求）。

### 5. 请求体没有 1 MiB 上限（SPEC §3、§7）

- **代码位置（修复前）**：`server/httpd.py:5` 虽定义了 `MAX_BODY`，但全文从未使用；`httpd.py:35-39` 的读循环对任意 `Content-Length` 都照单全收。
- **当前行为**：声明超大 Content-Length 或发送无限 chunked 流都会被读进内存，内存耗尽。
- **正确行为**：请求体超过 1 MiB 返回 `413 Payload Too Large`；且不得先把整个请求体读入内存再拒绝。
- **修复方式**：
  - Content-Length 路径在读 body 前先判断 `length > MAX_BODY`（`httpd.py:183`），直接 413，一个 body 字节都不读；
  - chunked 路径每块累计 `len(body) + size > MAX_BODY` 即 413（`httpd.py:211`），超限部分不读；
  - 413 与其他协议错误一样关闭连接，避免客户端继续在半解析状态上发送。
- **测试用例**：`test_body_oversize_content_length_returns_413`、`test_body_oversize_chunked_returns_413`（后者验证两个 512 KiB 块 + 16 字节即触发，且连接关闭）。

### 6. 不支持 Expect: 100-continue（SPEC §3）

- **代码位置（修复前）**：`server/httpd.py:27-40`，请求头解析后没有任何 Expect 处理，服务器直接阻塞读 body，永不发送 `100 Continue`。
- **当前行为**：带 `Expect: 100-continue` 的合规客户端会等待 interim 响应直到超时，大 body 上传场景直接卡死。
- **正确行为**：收到 `Expect: 100-continue` 后先回 `HTTP/1.1 100 Continue`，再读取请求体。
- **修复方式**：`_read_body()`（`httpd.py:187`）与 `_read_chunked()`（`httpd.py:194`）在读 body 前各发一次 `HTTP/1.1 100 Continue\r\n\r\n`；两条 body 路径都覆盖（已手动验证 chunked + Expect 组合）。
- **测试用例**：`test_expect_100_continue_is_honored`（先断言收到 100，再发 body，最后校验 200 回显）。

### 7. 不支持 keep-alive，Connection 头与实际行为不符（SPEC §4）

- **代码位置（修复前）**：`server/httpd.py:51-59`，`_build()` 对每个响应硬编码 `Connection: close`；`handle()` 处理完一个请求即返回，连接关闭。
- **当前行为**：HTTP/1.1 默认应为持久连接，但服务器一请求一关闭；响应头声称 close 与“本应 keep-alive”的协议语义不符，无法满足同连接两请求的验收。
- **正确行为**：默认 keep-alive 并循环处理多请求；客户端发 `Connection: close` 时响应后关闭；响应必须带与实际行为一致的 `Connection` 头。
- **修复方式**：
  - `handle()`（`httpd.py:85`）改为请求循环，`_handle_one()` 返回 keep-alive 标志；空闲等下一请求时使用 1 秒短超时（`KEEPALIVE_IDLE_TIMEOUT`），超时安静关闭，避免空闲线程长期挂住；
  - `_wants_keep_alive()`（`httpd.py:160` 附近）识别 `Connection: close`；
  - `_build()` 按实际行为输出 `Connection: keep-alive|close`（`httpd.py:229`）。
- **测试用例**：`test_keepalive_serves_two_requests_on_one_connection`、`test_connection_close_header_closes_socket`。

### 8. 不支持流水线，响应乱序/丢包（SPEC §6、§8）

- **代码位置（修复前）**：`server/httpd.py:13-19` 与 `35-42`：单次 `handle()` 只处理一个请求；`recv` 预读到的后续请求字节在 `body[:length]` 截断后随连接关闭丢弃。
- **当前行为**：客户端在一个连接上连续发送多个请求时，只有第一个被处理，其余字节被吞，连接关闭。
- **正确行为**：按请求顺序逐个返回响应，不串包、不丢弃。
- **修复方式**：§7 的请求循环 + `BufferedSocket` 缓冲共同解决——所有读取都先消费缓冲，预读字节天然成为下一个请求的开头；响应在各自请求处理完后立即按序发送，单连接内不存在并发，顺序由读取顺序保证。
- **测试用例**：`test_pipelining_responses_stays_in_order`（4 个流水线请求，含 200/404/POST 混合，逐响应核对状态码与 body）、`test_content_length_does_not_overread_pipelined_request`。

### 9. 响应缺少 Date 头（SPEC §5）

- **代码位置（修复前）**：`server/httpd.py:53-58`，响应头只有状态行、Content-Length、Connection。
- **当前行为**：任何响应都不带 `Date`，不符合 HTTP/1.1 对 Date 的要求。
- **正确行为**：每个响应带 `Date`，格式为 RFC 1123（GMT）。
- **修复方式**：用标准库 `email.utils.formatdate(usegmt=True)` 生成时间戳，在 `_build()`（`httpd.py:229`）和 `_send_error()`（`httpd.py:250`）中统一输出，保证正常响应与错误响应都带 Date。
- **测试用例**：`test_every_response_has_rfc1123_date`（用 `email.utils.parsedate_to_datetime` 校验格式可解析）。

### 10. 不支持 HEAD（SPEC §6）

- **代码位置（修复前）**：`server/httpd.py:44-49`，`_route()` 没有 HEAD 分支，`HEAD /` 返回 404 且带响应体。
- **当前行为**：HEAD 请求状态码错误且泄漏响应体。
- **正确行为**：返回与 GET 相同的状态行与响应头（含相同 Content-Length），但不得返回响应体；HEAD 后连接仍可复用。
- **修复方式**：`_route()` 让 HEAD 与 GET 走同一路由（`httpd.py:222` 附近）；`_build(..., head_only=True)` 只发头块（`httpd.py:229`），Content-Length 仍按 GET 本应返回的 body 长度填写。
- **测试用例**：`test_head_has_headers_but_no_body`（对比同路径 GET 的 Content-Length、断言零长度 body、并验证连接可继续复用）。

---

## 验证记录

- 修复前：`pytest -q` → `13 failed, 1 passed`（仅 `test_connection_close_header_closes_socket` 在旧实现的“始终 close”行为下偶然通过，修复后仍通过）。
- 修复后：`pytest -q` → `18 passed`，连续重复运行 3 轮结果一致，无 flaky。
- 额外手动验证（裸 socket）：畸形请求行 → 400 并关闭；chunked 带 trailer 正常解码；405 后同连接继续请求成功；`Expect: 100-continue` + chunked 组合正常；默认监听配置仍为 `127.0.0.1:8080`。

## 未改动项（按要求）

- `docs/SPEC.md`：未修改。
- `tests/test_functional.py`：断言与用例均未修改，4 个功能用例持续全绿。
- 监听地址、端口等运行配置：`make_server(host="127.0.0.1", port=8080)` 签名与默认值保持原样。
- 依赖：仅使用 Python 标准库。
