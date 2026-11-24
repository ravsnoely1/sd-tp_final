import asyncio
import json
import logging

from aiohttp import web

from .storage import DomainError

log = logging.getLogger("chat")


@web.middleware
async def errors(request, handler):
    try:
        response = await handler(request)
    except DomainError as exc:
        response = web.json_response({"error": str(exc)}, status=exc.status)
    except (json.JSONDecodeError, UnicodeDecodeError):
        response = web.json_response({"error": "JSON inválido."}, status=400)
    except web.HTTPException:
        raise
    except Exception:
        # Não registrar corpo, senha, token ou string de conexão.
        log.error("Falha interna: método=%s rota=%s", request.method, request.path)
        response = web.json_response({"error": "Serviço temporariamente indisponível."}, status=503)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response


async def body(request):
    result = await request.json()
    if not isinstance(result, dict):
        raise DomainError("O corpo deve ser um objeto JSON.")
    return result


def bearer(request):
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token or len(token) > 256:
        raise DomainError("Autenticação necessária.", 401)
    return token


async def call(method, *args, **kwargs):
    return await asyncio.to_thread(method, *args, **kwargs)
