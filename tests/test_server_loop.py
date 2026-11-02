"""The configured Windows server loop survives peer disconnects and releases its sockets.

Only a synthetic ASGI app on a guarded ephemeral loopback port: no TOW runtime or client.
"""

import asyncio
import contextlib
import http.server
import socket
import struct

import uvicorn

from tow import platform


async def _answer(scope, receive, send):
    assert scope["type"] == "http"
    await receive()
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"2")]})
    await send({"type": "http.response.body", "body": b"ok"})


async def _request(reader, writer):
    writer.write(b"GET /synthetic HTTP/1.1\r\nHost: localhost\r\n\r\n")
    await writer.drain()
    headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    assert headers.startswith(b"HTTP/1.1 200 ")
    assert await asyncio.wait_for(reader.readexactly(2), 5) == b"ok"


async def _connect(address):
    return await asyncio.wait_for(asyncio.open_connection(*address), 5)


async def _close(writer):
    writer.close()
    await writer.wait_closed()


async def _disconnects(address):
    # Two requests on one connection prove that keep-alive works before an orderly close.
    reader, writer = await _connect(address)
    await _request(reader, writer)
    await _request(reader, writer)
    await _close(writer)

    # A peer can disappear before it has sent even one complete HTTP header.
    _reader, writer = await _connect(address)
    writer.write(b"GET /synthetic HTTP/1.1\r\nHost:")
    await writer.drain()
    await _close(writer)

    loop = asyncio.get_running_loop()
    for _ in range(20):
        with socket.socket() as peer:
            peer.setblocking(False)
            await loop.sock_connect(peer, address)
            await loop.sock_sendall(peer, b"GET /synthetic HTTP/1.1\r\nHost:")
            linger = struct.pack("hh" if platform.this_os() == "windows" else "ii", 1, 0)
            peer.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
        # An abortive close must not end the listener or break the next response.
        reader, writer = await _connect(address)
        await _request(reader, writer)
        await _close(writer)


def test_selector_factory_handles_disconnects_and_shuts_down(loopback_http_server, monkeypatch, caplog):
    # Use the shared guarded-port allocator; its HTTP handler is never started.
    listener = loopback_http_server(http.server.BaseHTTPRequestHandler)
    config = uvicorn.Config(
        _answer,
        loop="asyncio:SelectorEventLoop",
        lifespan="off",
        access_log=False,
        log_config=None,
        timeout_graceful_shutdown=2,
    )
    factory = config.get_loop_factory()
    assert factory is asyncio.SelectorEventLoop
    server = uvicorn.Server(config)
    monkeypatch.setattr(server, "capture_signals", contextlib.nullcontext)
    errors = []

    async def exercise():
        loop = asyncio.get_running_loop()
        assert isinstance(loop, asyncio.SelectorEventLoop)
        loop.set_exception_handler(lambda _loop, context: errors.append(context))
        serving = asyncio.create_task(server.serve(sockets=[listener.socket]))

        async def ready():
            while not server.started:
                if serving.done():
                    await serving
                    raise AssertionError("the server ended before startup")
                await asyncio.sleep(0)

        async def released():
            while server.server_state.connections or server.server_state.tasks:
                await asyncio.sleep(0)

        try:
            await asyncio.wait_for(ready(), 5)
            await _disconnects(listener.server_address)
            await asyncio.wait_for(released(), 5)
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, 5)

    try:
        asyncio.run(exercise(), loop_factory=factory)
    finally:
        listener.server_close()
    assert not errors
    assert not server.server_state.connections
    assert not server.server_state.tasks
    assert not [record for record in caplog.records if record.levelno >= 40]
