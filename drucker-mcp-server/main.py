"""Drucker MCP server.

Prints to the Dell C2665dnf (or any IPP Everywhere printer that takes PDF) via
a built-in minimal IPP client — no CUPS, no driver. Files come inline (base64),
from a public HTTPS URL, from Paperless-ngx or from Nextcloud; images and plain
text are converted to PDF first.

Transport/auth (``_run_sse``) is adapted from signal-mcp / whatsapp-mcp:
Streamable-HTTP + classic SSE, a static ``MCP_API_KEY`` bearer and Authelia OIDC
introspection, plus the RFC 9728/8414 OAuth discovery endpoints Claude.ai needs.
"""

import logging
import os
from typing import Any, Callable, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

import convert
import ipp
import sources

MAX_PAGES = int(os.getenv("MAX_PAGES_PER_JOB", "50"))
MAX_COPIES = int(os.getenv("MAX_COPIES", "20"))

PAPER = {
    "A4": "iso_a4_210x297mm",
    "A5": "iso_a5_148x210mm",
    "B5": "iso_b5_176x250mm",
    "LETTER": "na_letter_8.5x11in",
    "LEGAL": "na_legal_8.5x14in",
}
DUPLEX = {
    "off": "one-sided",
    "long-edge": "two-sided-long-edge",
    "short-edge": "two-sided-short-edge",
}
COLOR = {"auto", "color", "monochrome"}

log = logging.getLogger("drucker-mcp")
mcp = FastMCP("drucker")


def _print(
    fetch: Callable[[], sources.Fetched],
    copies: int,
    duplex: str,
    color: str,
    paper: str,
    page_ranges: Optional[str],
    allow_large_job: bool,
    job_name: Optional[str],
) -> Dict[str, Any]:
    # Validate options before fetching anything.
    if not 1 <= copies <= MAX_COPIES:
        return {"success": False, "error": f"copies muss zwischen 1 und {MAX_COPIES} liegen."}
    sides = DUPLEX.get(duplex)
    if not sides:
        return {"success": False, "error": f"duplex muss eines von {sorted(DUPLEX)} sein."}
    if color not in COLOR:
        return {"success": False, "error": f"color muss eines von {sorted(COLOR)} sein."}
    media = PAPER.get(paper.upper())
    if not media:
        return {"success": False, "error": f"paper muss eines von {sorted(PAPER)} sein."}

    try:
        data, filename, content_type = fetch()
        pdf, pages, kind = convert.to_printable_pdf(data, filename, content_type, page_ranges)
    except (sources.SourceError, convert.ConversionError) as e:
        return {"success": False, "error": str(e)}

    total = pages * copies
    if total > MAX_PAGES and not allow_large_job:
        return {
            "success": False,
            "error": (
                f"Auftrag hätte {total} Seiten ({pages} × {copies} Kopien) und liegt damit über "
                f"dem Limit von {MAX_PAGES}. Nutzer fragen und bei Zustimmung mit "
                "allow_large_job=true erneut senden, oder page_ranges einschränken."
            ),
            "pages": pages,
            "copies": copies,
        }

    name = job_name or filename
    try:
        result = ipp.print_job(pdf, name, copies=copies, sides=sides, color_mode=color, media=media)
    except ipp.IPPError as e:
        return {"success": False, "error": f"Drucker hat den Auftrag abgelehnt: {e}"}
    except Exception as e:  # network errors etc.
        return {"success": False, "error": f"Drucker nicht erreichbar: {e}"}

    log.info("[print] job %s '%s' kind=%s pages=%d copies=%d sides=%s color=%s media=%s",
             result.get("job_id"), name, kind, pages, copies, sides, color, media)
    return {
        "success": True,
        **result,
        "document": name,
        "source_type": kind,
        "pages": pages,
        "copies": copies,
        "duplex": duplex,
        "color": color,
        "paper": paper.upper(),
    }


_OPTIONS_DOC = """    copies: Anzahl Kopien (1–20, Standard 1).
    duplex: "off" (einseitig, Standard), "long-edge" (beidseitig, Buchbindung)
            oder "short-edge" (beidseitig, Kalenderbindung).
    color: "auto" (Standard), "color" oder "monochrome" (Schwarzweiß, spart Farbtoner).
    paper: "A4" (Standard), "A5", "B5", "Letter", "Legal".
    page_ranges: Nur bestimmte Seiten, z. B. "1-3,5" oder "2-" (bis Ende).
    allow_large_job: Nur auf ausdrücklichen Wunsch des Nutzers true setzen —
            hebt das Limit von 50 Seiten (Seiten × Kopien) pro Auftrag auf.
    job_name: Optionaler Auftragsname fürs Druckerdisplay (Standard: Dateiname).
"""


def _with_options_doc(fn: Callable) -> Callable:
    fn.__doc__ = (fn.__doc__ or "").rstrip() + "\n" + _OPTIONS_DOC
    return fn


# --------------------------------------------------------------------------
# Print tools
# --------------------------------------------------------------------------
@mcp.tool()
@_with_options_doc
def print_file(
    content_base64: str,
    filename: str,
    copies: int = 1,
    duplex: str = "off",
    color: str = "auto",
    paper: str = "A4",
    page_ranges: Optional[str] = None,
    allow_large_job: bool = False,
    job_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Druckt eine direkt übergebene Datei (base64) auf dem Hausdrucker (Dell C2665dnf, Farblaser).

    Unterstützt PDF, Bilder (JPG/PNG/GIF/TIFF/WebP/HEIC — werden auf A4 eingepasst)
    und reinen Text. Office-Dateien vorher in PDF umwandeln.

    Args:
        content_base64: Dateiinhalt als base64 (data:-URLs werden akzeptiert).
        filename: Dateiname inkl. Endung, z. B. "rechnung.pdf" (hilft bei der Formaterkennung).
"""
    return _print(lambda: sources.from_base64(content_base64, filename),
                  copies, duplex, color, paper, page_ranges, allow_large_job, job_name)


@mcp.tool()
@_with_options_doc
def print_url(
    url: str,
    copies: int = 1,
    duplex: str = "off",
    color: str = "auto",
    paper: str = "A4",
    page_ranges: Optional[str] = None,
    allow_large_job: bool = False,
    job_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Lädt eine Datei von einer öffentlichen https://-URL und druckt sie.

    Interne/private Adressen sind gesperrt — dafür print_paperless_document oder
    print_nextcloud_file verwenden. Formate: PDF, Bilder, reiner Text (max. 50 MB).

    Args:
        url: Öffentliche https://-URL der Datei.
"""
    return _print(lambda: sources.from_url(url),
                  copies, duplex, color, paper, page_ranges, allow_large_job, job_name)


@mcp.tool()
@_with_options_doc
def print_paperless_document(
    document_id: int,
    instance: str = "",
    original: bool = False,
    copies: int = 1,
    duplex: str = "off",
    color: str = "auto",
    paper: str = "A4",
    page_ranges: Optional[str] = None,
    allow_large_job: bool = False,
    job_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Druckt ein Dokument aus Paperless-ngx anhand seiner Dokument-ID.

    Die ID stammt z. B. aus der Suche im Paperless-MCP. Verfügbare Instanzen
    zeigt printer_status unter "sources".

    Args:
        document_id: Paperless-Dokument-ID.
        instance: Paperless-Instanz ("privat" = paperless.bay-ram.de,
                  "geb" = paperless-geb.bay-ram.de / Energieberatung).
        original: true = Originaldatei statt der archivierten (OCR-)PDF drucken.
"""
    return _print(lambda: sources.from_paperless(document_id, instance, original),
                  copies, duplex, color, paper, page_ranges, allow_large_job, job_name)


@mcp.tool()
@_with_options_doc
def print_nextcloud_file(
    path: str,
    instance: str = "",
    copies: int = 1,
    duplex: str = "off",
    color: str = "auto",
    paper: str = "A4",
    page_ranges: Optional[str] = None,
    allow_large_job: bool = False,
    job_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Druckt eine Datei aus Nextcloud anhand ihres Pfads.

    Formate: PDF, Bilder, reiner Text. Den Pfad liefert z. B. das Nextcloud-MCP
    (list_files/search_files).

    Args:
        path: Pfad relativ zum Nextcloud-Home, z. B. "Dokumente/Formular.pdf".
        instance: Nextcloud-Instanz ("privat" = nextcloud.bay-ram.de,
                  "geb" = nextcloud-geb.bay-ram.de / Energieberatung).
"""
    return _print(lambda: sources.from_nextcloud(path, instance),
                  copies, duplex, color, paper, page_ranges, allow_large_job, job_name)


# --------------------------------------------------------------------------
# Status / queue tools
# --------------------------------------------------------------------------
@mcp.tool()
def printer_status() -> Dict[str, Any]:
    """Zustand des Druckers: bereit/druckt/gestoppt, Fehlerursachen (Papierstau,
    Papier leer, …), Tonerstände in Prozent, Warteschlange, konfigurierte Quellen."""
    try:
        status = ipp.printer_status()
    except Exception as e:
        return {"reachable": False, "error": str(e), "printer_uri": ipp.PRINTER_URI}
    status["reachable"] = True
    status["limits"] = {"max_pages_per_job": MAX_PAGES, "max_copies": MAX_COPIES}
    status["sources"] = {
        "paperless": sorted(sources.paperless_instances()),
        "nextcloud": sorted(sources.nextcloud_instances()),
    }
    return status


@mcp.tool()
def list_print_jobs(which: str = "not-completed", limit: int = 20) -> List[Dict[str, Any]]:
    """Druckaufträge am Drucker auflisten.

    Args:
        which: "not-completed" (Warteschlange, Standard) oder "completed" (Verlauf).
        limit: Maximale Anzahl (Standard 20).
    """
    if which not in ("not-completed", "completed"):
        which = "not-completed"
    return ipp.get_jobs(which, max(1, min(limit, 100)))


@mcp.tool()
def get_print_job(job_id: int) -> Dict[str, Any]:
    """Status eines einzelnen Druckauftrags (pending/processing/completed/aborted/…)."""
    try:
        return ipp.get_job(job_id)
    except ipp.IPPError as e:
        return {"job_id": job_id, "error": str(e)}


@mcp.tool()
def cancel_print_job(job_id: int) -> Dict[str, Any]:
    """Bricht einen noch nicht fertigen Druckauftrag ab."""
    try:
        ipp.cancel_job(job_id)
    except ipp.IPPError as e:
        return {"success": False, "job_id": job_id, "error": str(e)}
    return {"success": True, "job_id": job_id, "state": ipp.get_job(job_id).get("state")}


# --------------------------------------------------------------------------
# HTTP transport + auth (adapted from signal-mcp / whatsapp-mcp)
# --------------------------------------------------------------------------
def _run_sse() -> None:
    import contextlib
    import logging
    import os
    import time
    from collections.abc import AsyncIterator

    import httpx as _httpx
    import uvicorn
    from mcp.server.sse import SseServerTransport
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Mount, Route

    _log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    _level_int = getattr(logging, _log_level, logging.INFO)
    logging.basicConfig(
        level=_level_int,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        force=True,
    )
    logging.getLogger().setLevel(_level_int)
    log = logging.getLogger("drucker-mcp")
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).setLevel(_level_int)

    mcp_api_key = os.getenv("MCP_API_KEY", "")
    oidc_introspection_url = os.getenv("OIDC_INTROSPECTION_URL", "")
    oidc_client_id = os.getenv("OIDC_CLIENT_ID", "")
    oidc_client_secret = os.getenv("OIDC_CLIENT_SECRET", "")
    oauth_issuer = os.getenv("OAUTH_ISSUER", "")
    mcp_server_url = os.getenv("MCP_SERVER_URL", "")

    auth_configured = bool(mcp_api_key) or all(
        (oidc_introspection_url, oidc_client_id, oidc_client_secret)
    )
    # Fail closed: a publicly reachable printer must never run without auth.
    # ALLOW_NO_AUTH=true is only for local development.
    if not auth_configured:
        if os.getenv("ALLOW_NO_AUTH", "").lower() not in ("1", "true", "yes"):
            raise SystemExit(
                "[auth] Neither MCP_API_KEY nor a complete OIDC triple configured — "
                "refusing to start (set ALLOW_NO_AUTH=true for local dev only)."
            )
        log.warning("[auth] ALLOW_NO_AUTH set — ALL requests pass unauthenticated.")

    def _auth_preview(auth: str) -> str:
        return "(none)" if not auth else auth[:20] + ("…" if len(auth) > 20 else "")

    async def _is_authorized(request: Request) -> tuple[bool, Optional[str]]:
        tag = f"{request.method} {request.url.path}"
        auth = request.headers.get("Authorization", "")
        if not mcp_api_key and not (
            oidc_introspection_url and oidc_client_id and oidc_client_secret
        ):
            return True, None
        if not auth:
            log.warning("[auth] %s — DENY: no Authorization header", tag)
            return False, "no_header"
        if mcp_api_key and auth == f"Bearer {mcp_api_key}":
            log.info("[auth] %s — OK: static MCP_API_KEY", tag)
            return True, None
        if not auth.startswith("Bearer "):
            return False, "invalid_token"
        if not (oidc_introspection_url and oidc_client_id and oidc_client_secret):
            return False, "invalid_token"
        jwt_token = auth[7:]
        try:
            async with _httpx.AsyncClient(timeout=5.0) as http:
                resp = await http.post(
                    oidc_introspection_url,
                    data={"token": jwt_token},
                    auth=(oidc_client_id, oidc_client_secret),
                )
                if resp.status_code != 200:
                    log.warning("[auth] %s — DENY: introspection HTTP %s", tag, resp.status_code)
                    return False, "invalid_token"
                if bool(resp.json().get("active")):
                    log.info("[auth] %s — OK: OIDC token active", tag)
                    return True, None
                return False, "invalid_token"
        except Exception as e:
            log.error("[auth] %s — introspection error: %s", tag, e)
            return False, "invalid_token"

    def _unauthorized(reason: Optional[str]) -> Response:
        if reason == "invalid_token":
            www = (
                'Bearer realm="drucker-mcp", error="invalid_token", '
                'error_description="The access token expired or is invalid"'
            )
            return Response("Unauthorized", status_code=401,
                            headers={"WWW-Authenticate": www})
        return Response("Unauthorized", status_code=401)

    class RequestLogMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):  # type: ignore[override]
            t0 = time.monotonic()
            log.debug("→ %s %s auth=%s", request.method, request.url.path,
                      _auth_preview(request.headers.get("Authorization", "")))
            response = await call_next(request)
            log.debug("← %s %s → %d in %dms", request.method, request.url.path,
                      response.status_code, int((time.monotonic() - t0) * 1000))
            return response

    sse = SseServerTransport("/messages/")
    _server = mcp._mcp_server
    session_manager = StreamableHTTPSessionManager(app=_server, json_response=True)

    class _AlreadySent(Response):
        def __init__(self) -> None:
            super().__init__(content=b"", status_code=200)

        async def __call__(self, scope, receive, send):
            return

    async def handle_streamable_http(request: Request):
        ok, reason = await _is_authorized(request)
        if not ok:
            return _unauthorized(reason)
        await session_manager.handle_request(request.scope, request.receive, request._send)
        return _AlreadySent()

    @contextlib.asynccontextmanager
    async def lifespan(_app: "Starlette") -> AsyncIterator[None]:
        async with session_manager.run():
            log.info("StreamableHTTPSessionManager started")
            yield

    async def handle_sse(request: Request):
        ok, reason = await _is_authorized(request)
        if not ok:
            return _unauthorized(reason)
        async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
            await _server.run(streams[0], streams[1], _server.create_initialization_options())
        return Response()

    async def handle_messages(scope, receive, send):
        req = Request(scope, receive=receive)
        ok, reason = await _is_authorized(req)
        if not ok:
            await _unauthorized(reason)(scope, receive, send)
            return
        await sse.handle_post_message(scope, receive, send)

    async def handle_oauth_protected_resource(request: Request):
        return JSONResponse({
            "resource": mcp_server_url or str(request.base_url).rstrip("/"),
            "authorization_servers": [oauth_issuer] if oauth_issuer else [],
            "bearer_methods_supported": ["header"],
            "scopes_supported": ["openid", "profile", "email"],
        })

    async def handle_oauth_authorization_server(request: Request):
        if oauth_issuer:
            try:
                async with _httpx.AsyncClient(timeout=5.0) as http:
                    up = await http.get(f"{oauth_issuer}/.well-known/oauth-authorization-server")
                    if up.status_code == 200:
                        return JSONResponse(up.json())
            except Exception as e:
                log.warning("[discovery] upstream fetch failed: %s", e)
        return JSONResponse({
            "issuer": oauth_issuer,
            "authorization_endpoint": f"{oauth_issuer}/api/oidc/authorization",
            "token_endpoint": f"{oauth_issuer}/api/oidc/token",
            "jwks_uri": f"{oauth_issuer}/jwks.json",
            "introspection_endpoint": f"{oauth_issuer}/api/oidc/introspection",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": ["openid", "profile", "email"],
        })

    app = Starlette(
        routes=[
            Route("/.well-known/oauth-protected-resource",
                  endpoint=handle_oauth_protected_resource, methods=["GET"]),
            Route("/.well-known/oauth-authorization-server",
                  endpoint=handle_oauth_authorization_server, methods=["GET"]),
            Route("/sse", endpoint=handle_streamable_http, methods=["POST"]),
            Route("/mcp", endpoint=handle_streamable_http, methods=["POST"]),
            Route("/sse", endpoint=handle_sse, methods=["GET"]),
            Mount("/messages/", app=handle_messages),
        ],
        middleware=[Middleware(RequestLogMiddleware)],
        lifespan=lifespan,
    )

    port = int(os.getenv("PORT", "8000"))
    log.info("drucker-mcp listening on :%d (LOG_LEVEL=%s)", port, _log_level)
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    import os

    if os.getenv("MCP_TRANSPORT", "stdio") == "sse":
        _run_sse()
    else:
        mcp.run(transport="stdio")
