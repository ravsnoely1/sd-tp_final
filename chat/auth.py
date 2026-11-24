"""Serviço independente de usuários e autenticação (porta 8001)."""

import asyncio
import os

from aiohttp import web

from .http import bearer, body, call, errors
from .storage import Store


def create_app(store=None):
    store = store or Store(os.getenv("DATABASE_URL", "sqlite:///data/chat.db"))
    app = web.Application(middlewares=[errors], client_max_size=16 * 1024)
    # Evita dezenas de hashes scrypt simultâneos esgotando a memória.
    password_slots = asyncio.Semaphore(4)

    async def startup(app):
        await call(store.initialize, "auth")

    async def health(request):
        await call(store.healthy)
        return web.json_response({"status": "ok", "service": "auth"})

    async def register(request):
        data = await body(request)
        async with password_slots:
            user = await call(store.register, data.get("username"), data.get("password"))
        return web.json_response(user, status=201)

    async def login(request):
        data = await body(request)
        async with password_slots:
            session = await call(store.login, data.get("username"), data.get("password"))
        return web.json_response(session)

    async def me(request):
        return web.json_response(await call(store.authenticate, bearer(request)))

    async def logout(request):
        await call(store.logout, bearer(request))
        return web.json_response({"ok": True})

    async def users(request):
        await call(store.authenticate, bearer(request))
        return web.json_response({"users": await call(store.users)})

    app.on_startup.append(startup)
    app.add_routes([
        web.get("/health", health), web.post("/api/auth/register", register),
        web.post("/api/auth/login", login), web.post("/api/auth/logout", logout),
        web.get("/api/auth/me", me), web.get("/api/auth/users", users),
    ])
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8001")), access_log=None)
