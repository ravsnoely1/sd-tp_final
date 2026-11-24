"""Inicia processos independentes para demonstração local, sem Docker."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent


def wait_for(url, process):
    for _ in range(150):
        if process.poll() is not None:
            raise RuntimeError(f"Processo terminou antes de ficar pronto: {url}")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"Tempo de inicialização excedido: {url}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replicas", type=int, default=2)
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    if not 1 <= args.replicas <= 10 or 8001 <= args.port <= 8001 + args.replicas:
        parser.error("Use 1 a 10 réplicas e uma porta de gateway fora das portas 8001 a 8011.")
    children = []

    def start(module, port, extra=None):
        env = {**os.environ, "HOST": "127.0.0.1", "PORT": str(port), "PYTHONUNBUFFERED": "1", **(extra or {})}
        process = subprocess.Popen([sys.executable, "-m", module], cwd=ROOT, env=env)
        children.append(process)
        wait_for(f"http://127.0.0.1:{port}/health", process)
        return process

    try:
        start("chat.auth", 8001)
        for i in range(args.replicas):
            start("chat.server", 8002 + i, {"INSTANCE_ID": f"chat-{i + 1}"})
        urls = ",".join(f"http://127.0.0.1:{8002 + i}" for i in range(args.replicas))
        start("chat.gateway", args.port, {"CHAT_URLS": urls})
        print(f"\nProsa disponível em http://localhost:{args.port} — {args.replicas} réplicas. Ctrl+C encerra todos os processos.", flush=True)
        while all(p.poll() is None for p in children):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for process in reversed(children):
            if process.poll() is None:
                process.terminate()
        for process in children:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()
