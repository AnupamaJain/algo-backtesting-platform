"""One port. The terminal and the lab are reached through this process.

Three programs in two languages each listened on their own port, and a
reader had three URLs for what is meant to be one product. This mounts the
other two under path prefixes of this one:

    /terminal/*   ->  the Flask trading terminal   (loopback :5010)
    /lab/*        ->  the Next.js strategy lab     (loopback :4300)

They stay separate processes, deliberately. The terminal runs broker
websockets, schedulers and strategy subprocesses; a crash there must not
take sign-in and research down with it, and each can be restarted alone.
What changes is that they now bind to 127.0.0.1 and are reachable only
through here -- one door, where before the lab was listening on the LAN.

HTTP only. Neither upstream needs a websocket from the browser: the Lab's
dev-mode hot reload is the one thing that goes without, and it is not a
product feature.
"""

from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

router = APIRouter(include_in_schema=False)

UPSTREAMS = {
    "terminal": os.getenv("PRAMANA_TERMINAL_UPSTREAM", "http://127.0.0.1:5010"),
    "lab": os.getenv("PRAMANA_LAB_UPSTREAM", "http://127.0.0.1:4300"),
}

#: Whether the prefix is stripped before forwarding. Flask does not know it
#: lives under /terminal and learns it from X-Forwarded-Prefix, so it gets
#: bare paths. Next.js is configured with basePath "/lab" and expects to be
#: asked for "/lab/..." -- strip that and every page is a 404.
STRIP_PREFIX = {"terminal": True, "lab": False}

#: Headers that describe one hop, not the message. Forwarding them breaks
#: the next hop's own framing.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "content-length", "host",
}

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=5.0), follow_redirects=False)
    return _client


def rewrite_location(value: str, prefix: str) -> str:
    """A redirect to a root-relative path gets the prefix put back.

    The upstream does not know it lives under /terminal: Flask redirects
    "/" to "/home". Left alone, that would send the reader to this
    process's "/home", which does not exist.
    """
    if value.startswith("/") and not value.startswith("//") and not value.startswith(prefix + "/") and value != prefix:
        return prefix + value
    return value


async def forward(request: Request, name: str, path: str) -> Response:
    base = UPSTREAMS[name]
    prefix = f"/{name}"
    # No slash is invented between the prefix and an empty path. Forwarding a
    # bare "/lab" as ".../lab/" met Next's basePath normalisation, which
    # answers 308 -> "/lab"; the gateway put the slash back, and the browser
    # gave up at ERR_TOO_MANY_REDIRECTS.
    if STRIP_PREFIX[name]:
        url = f"{base}/{path}"
    else:
        url = f"{base}{prefix}" + (f"/{path}" if path else "")
    if request.url.query:
        url = f"{url}?{request.url.query}"

    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
    headers["x-forwarded-prefix"] = prefix
    # httpx would otherwise add its own "Accept-Encoding: gzip, deflate" and
    # the upstream would compress -- for a client that never asked and might
    # not decode. The client's preference goes through as sent; none means
    # none.
    headers.setdefault("accept-encoding", "identity")
    headers["x-forwarded-host"] = request.headers.get("host", "")
    headers["x-forwarded-proto"] = request.url.scheme
    # The original Host goes through untouched. The terminal builds absolute
    # URLs from it -- the login page's next= parameter, for one -- and with
    # the upstream's own name substituted those pointed at :5010, which the
    # reader cannot reach.
    headers["host"] = request.headers.get("host", "")

    body = await request.body()
    try:
        upstream = await client().send(
            client().build_request(request.method, url, headers=headers, content=body),
            stream=True,
        )
    except httpx.HTTPError as exc:
        return Response(
            f"The {name} is not running behind this port ({base}): {exc.__class__.__name__}. "
            f"Start it with run-all.sh.",
            status_code=502, media_type="text/plain",
        )

    out = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP}
    if "location" in out:
        out["location"] = rewrite_location(out["location"], prefix)

    # A response whose body is already in memory -- a test transport, or a
    # client that read it -- cannot be streamed a second time. Everything
    # from a live upstream streams.
    if getattr(upstream, "_content", None) is not None:
        await upstream.aclose()
        return Response(upstream.content, status_code=upstream.status_code, headers=out)
    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=out,
        background=BackgroundTask(upstream.aclose),
    )


METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]


@router.api_route("/terminal", methods=METHODS)
@router.api_route("/terminal/{path:path}", methods=METHODS)
async def terminal(request: Request, path: str = "") -> Response:
    return await forward(request, "terminal", path)


@router.api_route("/lab", methods=METHODS)
@router.api_route("/lab/{path:path}", methods=METHODS)
async def lab(request: Request, path: str = "") -> Response:
    return await forward(request, "lab", path)
