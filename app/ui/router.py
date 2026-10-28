"""Admin console routes: one HTML page plus its static assets.

No build step, no external template engine dependency. All paths are
computed from this package's ``__file__`` so the package works identically
from a source checkout, an installed wheel, or a container image.

Static assets are served through a plain APIRoute (a ``StaticFiles``
mount does not survive ``include_router``, so a plain route does that job
plain route instead). The browser (not the server) owns authentication:
the console reads the JWT from localStorage and talks to /api/v1 directly,
so nothing under /ui needs server-side auth by design.
"""
import mimetypes
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse

UI_DIR = Path(__file__).resolve().parent
STATIC_DIR = UI_DIR / "static"
CONSOLE_HTML = UI_DIR / "templates" / "console.html"

router = APIRouter()


@router.get("/", include_in_schema=False)
async def index() -> RedirectResponse:
    """Land on the console."""
    return RedirectResponse(url="/ui", status_code=307)


@router.get("/ui", include_in_schema=False)
async def console() -> FileResponse:
    """Serve the single-page console."""
    return FileResponse(CONSOLE_HTML, media_type="text/html")


@router.get("/ui/static/{asset_path:path}", include_in_schema=False)
async def console_static(asset_path: str):
    """Serve console assets (css/js) from the package directory.

    Correct media type via ``mimetypes``; path traversal and nonexistent
    assets return 404 instead of raising inside the response.
    """
    base = STATIC_DIR.resolve()
    requested = (base / asset_path).resolve()
    if base != requested and base not in requested.parents:
        return PlainTextResponse("No such asset", status_code=404)
    if not requested.is_file():
        return PlainTextResponse("No such asset", status_code=404)
    media_type = mimetypes.guess_type(requested.name)[0] or "application/octet-stream"
    return FileResponse(requested, media_type=media_type)
