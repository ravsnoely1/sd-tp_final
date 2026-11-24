"""Proxy round-robin para laboratório sem Docker. No Compose usa-se Nginx."""

import asyncio
from contextlib import suppress
import itertools
import os

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector, WSMsgType, web


def create_app(backends=None, auth_url=None):
    backends = backends or os.getenv("CHAT_URLS", "http://127.0.0.1:8002,http://127.0.0.1:8003").split(",")
    auth_url = auth_url or os.getenv("AUTH_URL", "http://127.0.0.1:8001")
    rotation = itertools.cycle(range(len(backends)))
    state = {}

    async def lifecycle(app):
        # WebSockets ocupam conexões por toda a sessão; o limite padrão de 100
        # bloquearia chamadas HTTP quando o laboratório alcançasse 100 usuários.
        state["http"] = ClientSession(timeout=ClientTimeout(total=30), connector=TCPConnector(limit=0))
        yield
        await state["http"].close()

    async def pipe(source, target):
        async for frame in source:
            if frame.type == WSMsgType.TEXT:
                await target.send_str(frame.data)
            elif frame.type == WSMsgType.BINARY:
                await target.send_bytes(frame.data)
            elif frame.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                break

    async def proxy(request):
        start = next(rotation)
        candidates = [auth_url] if request.path.startswith("/api/auth/") else [backends[(start + i) % len(backends)] for i in range(len(backends))]
        # Não repetir POST automaticamente: o resultado de um commit pode ser incerto.
        for base in candidates:
            try:
                if request.path == "/ws":
                    upstream = await state["http"].ws_connect(base + "/ws", heartbeat=20)
                    downstream = web.WebSocketResponse(heartbeat=20)
                    await downstream.prepare(request)
                    tasks = [asyncio.create_task(pipe(upstream, downstream)), asyncio.create_task(pipe(downstream, upstream))]
                    try:
                        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        for task in tasks:
                            task.cancel()
                        for task in tasks:
                            with suppress(asyncio.CancelledError, ConnectionError):
                                await task
                        await upstream.close()
                        await downstream.close()
                    return downstream
                headers = {k: v for k, v in request.headers.items() if k.lower() in ("authorization", "content-type", "accept")}
                async with state["http"].request(request.method, base + request.rel_url.path_qs, data=await request.read(), headers=headers) as response:
                    result = await response.read()
                    return web.Response(body=result, status=response.status, headers={k: v for k, v in response.headers.items() if k.lower() in ("content-type", "cache-control", "x-content-type-options")})
            except (ClientError, OSError, asyncio.TimeoutError):
                if request.method != "GET":
                    break
        return web.json_response({"error": "Nenhuma réplica disponível."}, status=503)

    app = web.Application(client_max_size=32 * 1024)
    app.cleanup_ctx.append(lifecycle)
    app.router.add_route("*", "/{path:.*}", proxy)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8080")), access_log=None)
