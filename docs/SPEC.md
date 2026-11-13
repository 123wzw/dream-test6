# HTTP/1.1 协议要求（SPEC）

本文是验收的唯一依据。代码与本文不符即为缺陷。每条都可用裸 socket 复现。

## 1. 请求行

- 格式必须为 `METHOD SP request-target SP HTTP/1.1`。
- 方法必须为 `GET` 或 `POST`；其他方法返回 `405 Method Not Allowed`，并带 `Allow: GET, POST` 响应头。

## 2. 请求头

- 请求头总大小超过 **8192 字节**时，返回 `431 Request Header Fields Too Large`，并关闭连接。
- 请求行中的 request-target 长度超过 **2048 字节**时，返回 `414 URI Too Long`。

## 3. 请求体

- 带 `Content-Length` 时按该长度读取，不得多读。
- 带 `Transfer-Encoding: chunked` 时必须按 chunked 规则解码，解码结果与 `Content-Length` 语义等价。
- 请求体超过 **1 MiB** 时返回 `413 Payload Too Large`。
- 请求带 `Expect: 100-continue` 时，服务器必须先回 `100 Continue`，再读取请求体。

## 4. 连接管理

- HTTP/1.1 默认 `keep-alive`：同一连接可以连续处理多个请求。
- 客户端发送 `Connection: close` 时，响应后关闭连接。
- 响应必须带 `Connection: keep-alive` 或 `Connection: close`，与服务器实际行为一致。

## 5. 响应头

- 每个响应必须带 `Date`，格式为 RFC 1123（如 `Wed, 13 Nov 2026 21:00:00 GMT`）。
- 有响应体时必须带准确的 `Content-Length`。

## 6. 特殊方法与流水线

- `HEAD`：返回与 `GET` 相同的状态行与响应头，但**不得返回响应体**。
- 流水线（pipelining）：客户端在一个连接上连续发送多个请求、不等响应，服务器必须**按请求顺序**逐个返回响应，不得串包或丢弃。

## 7. 状态码

| 场景 | 状态码 |
|---|---|
| 正常 | 200 |
| 方法不支持 | 405 |
| 路径不存在 | 404 |
| 请求头过大 | 431 |
| URI 过长 | 414 |
| 请求体过大 | 413 |

## 8. 验收方式

- 用裸 socket 逐条构造请求，状态码、响应头、响应体都要符合上述规定。
- keep-alive 场景下，同一个 socket 上连续发两个请求，两次都要拿到正确响应。
- chunked 场景下，`POST /echo` 的响应体必须等于解码后的内容。
