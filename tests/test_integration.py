import asyncio
from pathlib import Path
import tempfile
import unittest

from aiohttp import ClientSession, web

from chat.auth import create_app as auth_app
from chat.gateway import create_app as gateway_app
from chat.server import create_app as chat_app
from chat.storage import Store


async def serve(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.temp.name) / "integration.db"))
        self.runners = []
        auth_runner, self.auth = await serve(auth_app(self.store))
        self.runners.append(auth_runner)
        chat1, self.node1 = await serve(chat_app(self.store, self.auth, "node-1"))
        chat2, self.node2 = await serve(chat_app(self.store, self.auth, "node-2"))
        self.runners.extend([chat1, chat2])
        gateway, self.gateway = await serve(gateway_app([self.node1, self.node2], self.auth))
        self.runners.append(gateway)
        self.client = ClientSession()

    async def asyncTearDown(self):
        await self.client.close()
        for runner in reversed(self.runners):
            await runner.cleanup()
        self.temp.cleanup()

    async def request(self, path, method="GET", data=None, token=None, base=None):
        async with self.client.request(method, (base or self.gateway) + path, json=data, headers={"Authorization": f"Bearer {token}"} if token else {}) as response:
            return response.status, await response.json()

    async def user(self, username):
        data = {"username": username, "password": "test-password"}
        status, user = await self.request("/api/auth/register", "POST", data)
        self.assertEqual(201, status)
        status, session = await self.request("/api/auth/login", "POST", data)
        self.assertEqual(200, status)
        return user, session["token"]

    async def ws(self, base, token):
        socket = await self.client.ws_connect(base + "/ws")
        await socket.send_json({"type": "auth", "token": token})
        ready = await socket.receive_json(timeout=5)
        self.assertEqual("ready", ready["type"])
        return socket

    async def receive_type(self, socket, kind):
        async with asyncio.timeout(5):
            while True:
                event = await socket.receive_json()
                if event["type"] == kind:
                    return event

    async def test_cross_replica_private_group_and_history(self):
        alice, ta = await self.user("alice")
        bob, tb = await self.user("bob")
        carol, tc = await self.user("carol")
        async with await self.ws(self.node1, ta) as a, await self.ws(self.node2, tb) as b, await self.ws(self.node2, tc) as c:
            for kind, members, name in [("direct", [bob["id"]], ""), ("group", [bob["id"], carol["id"]], "Equipe")]:
                status, room = await self.request("/api/chat/rooms", "POST", {"kind": kind, "name": name, "members": members}, ta)
                self.assertEqual(201, status)
                await a.send_json({"type": "send", "room_id": room["id"], "content": f"Olá {kind}", "client_id": kind})
                delivered = await self.receive_type(b, "message")
                self.assertEqual(f"Olá {kind}", delivered["message"]["content"])
                await self.receive_type(a, "ack")
                if kind == "group":
                    self.assertEqual(room["id"], (await self.receive_type(c, "message"))["message"]["room_id"])
                else:
                    status, _ = await self.request(f'/api/chat/rooms/{room["id"]}/messages', token=tc)
                    self.assertEqual(403, status)
                    with self.assertRaises(asyncio.TimeoutError):
                        await c.receive_json(timeout=0.2)
                status, history = await self.request(f'/api/chat/rooms/{room["id"]}/messages', token=tb)
                self.assertEqual(200, status)
                self.assertEqual(1, len(history["messages"]))

    async def test_authentication_validation_and_frontend(self):
        _, token = await self.user("alice")
        status, _ = await self.request("/api/chat/rooms")
        self.assertEqual(401, status)
        status, _ = await self.request("/api/chat/rooms", "POST", {"kind": "group", "name": "X", "members": ["missing"]}, token)
        self.assertEqual(400, status)
        status, _ = await self.request("/api/chat/rooms", "POST", [], token)
        self.assertEqual(400, status)
        async with self.client.get(self.gateway + "/") as response:
            self.assertEqual(200, response.status)
            self.assertIn('id="message-form"', await response.text())
        async with self.client.ws_connect(self.node1 + "/ws") as ws:
            await ws.send_json({"type": "auth", "token": "invalid"})
            self.assertEqual(401, (await ws.receive_json(timeout=5))["status"])

    async def test_balance_and_replica_failure_with_recovery(self):
        alice, token = await self.user("alice")
        bob, tb = await self.user("bob")
        room = self.store.create_room(alice, "direct", "", [bob])
        instances = set()
        for _ in range(6):
            async with await self.ws(self.gateway, token) as ws:
                # O ready é consumido por ws(); a distribuição também é observável em health.
                pass
            status, health = await self.request("/health")
            self.assertEqual(200, status)
            instances.add(health["instance"])
        # Consulta direta das conexões pelo gateway em sequência evita amostragem alternada.
        healths = [await self.request("/health") for _ in range(4)]
        self.assertEqual({"node-1", "node-2"}, {data["instance"] for _, data in healths})
        dead = await self.ws(self.node1, token)
        await self.runners[1].cleanup()
        await dead.close()
        async with await self.ws(self.gateway, token) as recovered, await self.ws(self.node2, tb) as recipient:
            await recovered.send_json({"type": "send", "room_id": room["id"], "content": "Após falha", "client_id": "recovered"})
            self.assertEqual("Após falha", (await self.receive_type(recipient, "message"))["message"]["content"])
        status, history = await self.request(f'/api/chat/rooms/{room["id"]}/messages', token=token)
        self.assertEqual(200, status)
        self.assertEqual("Após falha", history["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
