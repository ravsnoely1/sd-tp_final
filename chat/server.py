"""Serviço de mensagens replicável: WebSocket + log transacional compartilhado."""

import asyncio
from contextlib import suppress
import json
import logging
import os
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

from .http import bearer, body, call, errors
from .storage import DomainError, Store

log = logging.getLogger("chat")
STATIC = Path(__file__).resolve().parent.parent / "frontend"


def create_app(store=None, auth_url=None, instance=None):
    store = store or Store(os.getenv("DATABASE_URL", "sqlite:///data/chat.db"))
    auth_url = auth_url or os.getenv("AUTH_URL", "http://127.0.0.1:8001")
    instance = instance or os.getenv("INSTANCE_ID", os.getenv("HOSTNAME", "chat-local"))
    app = web.Application(middlewares=[errors], client_max_size=32 * 1024)
    sockets = {}  # WebSocket -> user_id; cada réplica mantém somente suas conexões.
    state = {"poll_healthy": True}

    async def auth(token, path="me"):
        try:
            async with state["http"].get(f"{auth_url}/api/auth/{path}", headers={"Authorization": f"Bearer {token}"}) as response:
                if response.status == 401:
                    raise DomainError("Sessão expirada. Entre novamente.", 401)
                if response.status != 200:
                    raise DomainError("Autenticação indisponível.", 503)
                return await response.json()
        except (OSError, asyncio.TimeoutError):
            raise DomainError("Autenticação indisponível.", 503) from None

    async def deliver(ws, payload):
        try:
            await asyncio.wait_for(ws.send_json(payload), timeout=2)
        except (ConnectionError, RuntimeError, asyncio.TimeoutError):
            await ws.close(code=1013, message=b"Reconnect and reload history")

    async def poll():
        cursor = state["cursor"]
        while True:
            try:
                events = await call(store.events, cursor)
                for event_id, payload, recipients in events:
                    await asyncio.gather(*(deliver(ws, payload) for ws, uid in list(sockets.items()) if uid in recipients))
                    cursor = event_id
                state["poll_healthy"] = True
                await asyncio.sleep(0 if len(events) == 250 else 0.08)
            except asyncio.CancelledError:
                raise
            except Exception:
                state["poll_healthy"] = False
                log.warning("Leitura de eventos indisponível: instância=%s", instance)
                await asyncio.sleep(1)

    async def lifecycle(app):
        await call(store.initialize, "chat")
        state["cursor"] = await call(store.event_head)
        state["http"] = ClientSession(timeout=ClientTimeout(total=5))
        task = asyncio.create_task(poll())
        yield
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await asyncio.gather(*(ws.close(code=1001, message=b"Server shutdown") for ws in list(sockets)))
        await state["http"].close()

    async def health(request):
        await call(store.healthy)
        status = 200 if state["poll_healthy"] else 503
        return web.json_response({"status": "ok" if status == 200 else "degraded", "instance": instance, "connections": len(sockets)}, status=status)

    async def list_rooms(request):
        user = await auth(bearer(request))
        return web.json_response({"rooms": await call(store.rooms, user["id"])})

    async def create_room(request):
        token = bearer(request)
        actor = await auth(token)
        data = await body(request)
        ids = data.get("members", [])
        if not isinstance(ids, list) or len(ids) > 100 or any(not isinstance(i, str) for i in ids):
            raise DomainError("Lista de participantes inválida.")
        available = {u["id"]: u for u in (await auth(token, "users"))["users"]}
        if any(i not in available for i in ids):
            raise DomainError("Participante não encontrado.")
        room = await call(store.create_room, actor, data.get("kind"), data.get("name", ""), [available[i] for i in ids])
        return web.json_response(room, status=201)

    async def history(request):
        actor = await auth(bearer(request))
        try:
            limit = int(request.query.get("limit", 50))
        except ValueError:
            raise DomainError("Limite inválido.") from None
        if not 1 <= limit <= 100:
            raise DomainError("O limite deve ficar entre 1 e 100.")
        messages = await call(store.messages, request.match_info["room_id"], actor["id"], request.query.get("before"), limit)
        return web.json_response({"messages": messages, "next_before": messages[0]["id"] if len(messages) == limit else None})

    async def websocket(request):
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=32 * 1024)
        await ws.prepare(request)
        try:
            first = await ws.receive_json(timeout=5)
            if not isinstance(first, dict) or first.get("type") != "auth" or not isinstance(first.get("token"), str):
                raise DomainError("Envie a autenticação na primeira mensagem.", 401)
            token = first["token"]
            actor = await auth(token)
            sockets[ws] = actor["id"]
            await ws.send_json({"type": "ready", "instance": instance, "user": actor})
            while not ws.closed:
                try:
                    frame = await ws.receive(timeout=60)
                except asyncio.TimeoutError:
                    await auth(token)
                    continue
                if frame.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                    break
                if frame.type != WSMsgType.TEXT:
                    continue
                client_id = None
                try:
                    data = json.loads(frame.data)
                    if not isinstance(data, dict):
                        raise DomainError("Mensagem inválida.")
                    client_id = data.get("client_id")
                    if data.get("type") != "send" or not isinstance(data.get("room_id"), str):
                        raise DomainError("Comando inválido.")
                    actor = await auth(token)
                    message = await call(store.send, actor, data["room_id"], data.get("content"), client_id)
                    await ws.send_json({"type": "ack", "message": message})
                except (DomainError, json.JSONDecodeError) as exc:
                    await ws.send_json({"type": "error", "error": str(exc), "client_id": client_id, "status": getattr(exc, "status", 400)})
                    if getattr(exc, "status", 400) == 401:
                        await ws.close(code=4001)
        except (DomainError, asyncio.TimeoutError, ValueError) as exc:
            await ws.send_json({"type": "error", "error": str(exc), "status": getattr(exc, "status", 401)})
            await ws.close(code=4001)
        finally:
            sockets.pop(ws, None)
        return ws

    async def auth_proxy(request):
        # Somente na execução local. No Compose, Nginx encaminha diretamente.
        headers = {"Authorization": request.headers.get("Authorization", ""), "Content-Type": "application/json"}
        async with state["http"].request(request.method, auth_url + request.path, data=await request.read(), headers=headers) as response:
            return web.Response(body=await response.read(), status=response.status, content_type="application/json")

    async def index(request):
        return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    async def shutdown(app):
        await asyncio.gather(*(ws.close(code=1001, message=b"Server shutdown") for ws in list(sockets)))

    app.on_shutdown.append(shutdown)
    app.cleanup_ctx.append(lifecycle)
    app.add_routes([
        web.get("/health", health), web.get("/api/chat/rooms", list_rooms),
        web.post("/api/chat/rooms", create_room), web.get("/api/chat/rooms/{room_id}/messages", history),
        web.get("/ws", websocket), web.route("*", "/api/auth/{action}", auth_proxy),
        web.get("/", index), web.static("/static/", STATIC),
    ])
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    web.run_app(create_app(), host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8002")), access_log=None)
