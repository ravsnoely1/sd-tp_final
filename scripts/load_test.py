"""Carga verificável: login concorrente, WebSockets simultâneos, grupos e histórico."""

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import statistics
import time

from aiohttp import ClientSession, ClientTimeout, TCPConnector


async def run(args):
    started = time.perf_counter()
    run_id = secrets.token_hex(4)
    password = secrets.token_urlsafe(18)
    sockets, readers = [], []
    sent_at, delivery_times, delivered, acknowledged = {}, [], set(), set()
    failures = []
    expected_deliveries = set()
    expected_per_room = {}
    instances = Counter()
    sem = asyncio.Semaphore(20)

    async with ClientSession(timeout=ClientTimeout(total=120), connector=TCPConnector(limit=0)) as client:
        async def request(path, method="GET", data=None, token=None):
            async with client.request(method, args.url.rstrip("/") + path, json=data, headers={"Authorization": f"Bearer {token}"} if token else {}) as response:
                result = await response.json()
                if response.status >= 400:
                    raise RuntimeError(f"HTTP {response.status} em {path}: {result.get('error', 'erro')}")
                return result

        async def register(i):
            data = {"username": f"lt_{run_id}_{i}", "password": password}
            async with sem:
                await request("/api/auth/register", "POST", data)
            return data

        async def login(data):
            # Todas as tarefas de login são iniciadas juntas; o serviço limita os hashes.
            return await request("/api/auth/login", "POST", data)

        async def receive(index, ws):
            try:
                async for frame in ws:
                    if frame.type.name != "TEXT":
                        continue
                    event = json.loads(frame.data)
                    if event["type"] == "error":
                        failures.append(event["error"])
                    elif event["type"] in ("message", "ack"):
                        cid = event["message"]["client_id"]
                        if event["type"] == "ack":
                            acknowledged.add(cid)
                        elif cid in sent_at and (index, cid) not in delivered:
                            delivered.add((index, cid))
                            delivery_times.append((time.perf_counter() - sent_at[cid]) * 1000)
            except Exception as exc:
                failures.append(f"Leitor {index}: {type(exc).__name__}")

        try:
            credentials = await asyncio.gather(*(register(i) for i in range(args.users)))
            login_started = time.perf_counter()
            sessions = await asyncio.gather(*(login(data) for data in credentials))
            login_seconds = time.perf_counter() - login_started

            async def connect(index):
                ws = await client.ws_connect(args.url.rstrip("/") + "/ws", heartbeat=20)
                await ws.send_json({"type": "auth", "token": sessions[index]["token"]})
                ready = await ws.receive_json(timeout=30)
                if ready.get("type") != "ready":
                    await ws.close()
                    raise RuntimeError("WebSocket não autenticado.")
                instances[ready["instance"]] += 1
                sockets.append(ws)
                readers.append(asyncio.create_task(receive(index, ws)))
                return ws

            ordered = await asyncio.gather(*(connect(i) for i in range(args.users)))
            groups = []
            for group in range(args.groups):
                indices = list(range(group, args.users, args.groups))
                owner = indices[0]
                room = await request("/api/chat/rooms", "POST", {"kind": "group", "name": f"Carga {run_id} · {group + 1}", "members": [sessions[i]["user"]["id"] for i in indices[1:]]}, sessions[owner]["token"])
                groups.append((room, indices))
                expected_per_room[room["id"]] = set()

            messaging_started = time.perf_counter()
            async def send(index, room, recipients, suffix):
                cid = f"load-{run_id}-{suffix}"
                expected_per_room[room["id"]].add(cid)
                expected_deliveries.update((recipient, cid) for recipient in recipients)
                sent_at[cid] = time.perf_counter()
                await ordered[index].send_json({"type": "send", "room_id": room["id"], "content": f"Carga {suffix}", "client_id": cid})

            for turn in range(args.rounds):
                await asyncio.gather(*(send(i, room, indices, f"g-{group}-u-{i}-r-{turn}") for group, (room, indices) in enumerate(groups) for i in indices))
            direct = await request("/api/chat/rooms", "POST", {"kind": "direct", "members": [sessions[-1]["user"]["id"]]}, sessions[0]["token"])
            expected_per_room[direct["id"]] = set()
            await send(0, direct, [0, args.users - 1], "private")

            deadline = time.perf_counter() + args.timeout
            while time.perf_counter() < deadline and (not expected_deliveries.issubset(delivered) or len(acknowledged) < len(sent_at)) and not failures:
                await asyncio.sleep(0.05)
            messaging_seconds = time.perf_counter() - messaging_started
            missing = expected_deliveries - delivered
            if missing:
                failures.append(f"{len(missing)} entregas ausentes.")
            if len(acknowledged) != len(sent_at):
                failures.append("Há envios sem confirmação de persistência.")

            # Confere IDs persistidos, percorrendo todas as páginas de cada conversa.
            for room, indices in [*groups, (direct, [0, args.users - 1])]:
                persisted, before = set(), None
                while True:
                    suffix = f"?limit=100&before={before}" if before else "?limit=100"
                    history = await request(f'/api/chat/rooms/{room["id"]}/messages{suffix}', token=sessions[indices[0]]["token"])
                    persisted.update(m["client_id"] for m in history["messages"])
                    before = history["next_before"]
                    if not before:
                        break
                if persisted != expected_per_room[room["id"]]:
                    failures.append("Histórico divergente das mensagens enviadas.")

            if len(instances) < args.min_replicas:
                failures.append(f"Esperadas {args.min_replicas} réplicas, observadas {len(instances)}.")
            ordered_times = sorted(delivery_times)
            report = {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(), "run_id": run_id,
                "target": args.url, "environment": args.label,
                "users_simultaneous": args.users, "groups": args.groups, "rounds": args.rounds,
                "connections_by_instance": dict(instances), "messages_sent": len(sent_at),
                "acks": len(acknowledged), "expected_deliveries": len(expected_deliveries),
                "deliveries": len(delivered & expected_deliveries), "history_verified": not any("Histórico" in f for f in failures),
                "latency_ms": {"median": round(statistics.median(ordered_times), 2) if ordered_times else None, "p95": round(ordered_times[min(len(ordered_times) - 1, int(len(ordered_times) * .95))], 2) if ordered_times else None},
                "login_seconds": round(login_seconds, 3), "messaging_seconds": round(messaging_seconds, 3),
                "total_seconds": round(time.perf_counter() - started, 3), "errors": failures, "passed": not failures,
            }
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0 if report["passed"] else 1
        except Exception as exc:
            report = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "run_id": run_id,
                      "environment": args.label, "passed": False,
                      "errors": [f"{type(exc).__name__}: {exc}"],
                      "users_requested": args.users, "connections_established": len(sockets)}
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 1
        finally:
            await asyncio.gather(*(ws.close() for ws in sockets), return_exceptions=True)
            for task in readers:
                task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--users", type=int, default=100)
    parser.add_argument("--groups", type=int, default=20)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--min-replicas", type=int, default=2)
    parser.add_argument("--label", default="Informar ambiente na execução")
    parser.add_argument("--output", type=Path, default=Path("results/load.json"))
    args = parser.parse_args()
    if args.users < 2 or args.groups < 1 or args.users < args.groups * 2 or args.rounds < 1:
        parser.error("Use pelo menos 2 usuários por grupo e uma rodada.")
    raise SystemExit(asyncio.run(run(args)))
