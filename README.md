# mini-http

一个自研的极简 HTTP/1.1 服务器，给内部工具做本地接口用。

## 技术栈

- Python 3.12，纯标准库（socket / socketserver）

## 运行

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt

# 启动，默认监听 127.0.0.1:8080
.venv/Scripts/python -m server.httpd
```

## 路由

| 方法 | 路径 | 行为 |
|---|---|---|
| GET | `/` | 返回 `OK` |
| POST | `/echo` | 原样返回请求体 |
| 其他 | 任意 | 404 |

## 协议要求

见 `docs/SPEC.md`，该文件是唯一的验收依据。

## 测试

```bash
.venv/Scripts/python -m pytest -q
```

## 已知问题

- 协议实现只覆盖了最基本的请求，很多 RFC 7230 的细节没做。
- 内置的 `http.client` 能跑通，但用 curl / 浏览器访问时偶发连接被重置。
