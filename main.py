from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import io
import tempfile
import os
import sys
import socket
import ipaddress
from urllib.parse import urlparse
import requests

# On Windows, ensure asyncio uses the Proactor event loop (supports subprocesses)
if sys.platform == "win32":
    import asyncio
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except AttributeError:
        pass

app = FastAPI(title="URL to PDF Converter API", version="2.0.0")

# ─── Runtime Configuration ────────────────────────────────────────────────────
RUN_MODE    = os.getenv("RUN_MODE", "render").lower()   # 'local' or 'render'
ANGULAR_URL = os.getenv("ANGULAR_URL", "http://localhost:4200")

# ─── CORS ─────────────────────────────────────────────────────────────────────
cors_origins = ["http://localhost:4200"]
if ANGULAR_URL and ANGULAR_URL not in cors_origins:
    cors_origins.append(ANGULAR_URL)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Request Schema ───────────────────────────────────────────────────────────
class ConvertRequest(BaseModel):
    url: str
    filename: str = "converted.pdf"
    wait_seconds: float = 4.0  # extra wait for JS-heavy/Angular pages


# ─── Helpers ──────────────────────────────────────────────────────────────────
def _is_private_hostname(hostname: str) -> bool:
    """Return True if the hostname resolves to a private/non-routable IP."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except Exception:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except Exception:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return True
    return False


def _is_publicly_accessible(url: str, timeout: int = 10) -> bool:
    """Check reachability by hitting just the base URL (ignores hash fragment)."""
    try:
        parsed   = urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        resp     = requests.get(base_url, timeout=timeout, allow_redirects=True)
        return resp.status_code < 500
    except Exception:
        return False


def _convert_with_playwright(url: str, tmp_path: str, wait_seconds: float) -> None:
    """
    Render URL → PDF using Playwright (sync, runs in a thread).

    Key fixes for Angular hash-routing (/#/path):
      - Uses 'domcontentloaded' instead of 'networkidle' for hash routes
        because SPAs keep background XHR polling that never reaches idle.
      - Waits `wait_seconds` for Angular to bootstrap & render.
      - Verifies the page has real content before capturing.
      - Scrolls to bottom to trigger any lazy-loaded content.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            java_script_enabled=True,
        )
        page = context.new_page()

        try:
            # Hash routes need domcontentloaded — networkidle hangs on SPAs
            wait_strategy = "domcontentloaded" if "#" in url else "networkidle"
            page.goto(url, wait_until=wait_strategy, timeout=60000)

            # Give Angular time to bootstrap and render components
            page.wait_for_timeout(int(wait_seconds * 1000))

            # Best-effort: confirm page has visible content (not blank)
            try:
                page.wait_for_function(
                    "document.body && document.body.innerText.trim().length > 50",
                    timeout=15000,
                )
            except Exception:
                pass  # proceed anyway; some pages render inside canvas/SVG

            # Scroll to bottom to trigger lazy-loaded content
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(500)

            page.pdf(
                path=tmp_path,
                format="A4",
                print_background=True,
                margin={
                    "top": "20px",
                    "bottom": "20px",
                    "left": "20px",
                    "right": "20px",
                },
            )
        finally:
            context.close()
            browser.close()


# ─── Routes ───────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {
        "message": "URL to PDF Converter API is running ✅",
        "version": "2.0.0",
        "run_mode": RUN_MODE,
    }


@app.get("/health")
def health_check():
    return {"status": "ok", "run_mode": RUN_MODE}


@app.post("/convert")
async def convert_url_to_pdf(request: ConvertRequest):
    """Convert any URL to a downloadable PDF."""
    import asyncio

    url = request.url.strip()

    # ── Validate URL scheme ───────────────────────────────────────────────────
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(
            status_code=400,
            detail="URL must start with http:// or https://",
        )

    hostname = parsed.hostname or ""

    # ── Block private IPs in render mode ─────────────────────────────────────
    if RUN_MODE == "render":
        if _is_private_hostname(hostname):
            raise HTTPException(
                status_code=400,
                detail=(
                    "URL is not publicly reachable from this server. "
                    "Ensure the URL is accessible from the public internet "
                    "or run the converter locally (RUN_MODE=local)."
                ),
            )
        if not _is_publicly_accessible(url):
            raise HTTPException(
                status_code=400,
                detail=(
                    "URL is not publicly reachable from this server. "
                    "Ensure the URL is accessible from the public internet "
                    "or run the converter locally."
                ),
            )

    # ── Convert ───────────────────────────────────────────────────────────────
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_file:
            tmp_path = tmp_file.name

        try:
            # Run sync Playwright in a thread — keeps FastAPI event loop free
            await asyncio.to_thread(
                _convert_with_playwright, url, tmp_path, request.wait_seconds
            )
        except ImportError:
            # Fallback: WeasyPrint (no JS — for simple static pages only)
            try:
                import weasyprint
                weasyprint.HTML(url=url).write_pdf(tmp_path)
            except ImportError:
                raise HTTPException(
                    status_code=500,
                    detail=(
                        "No PDF engine found. "
                        "Install: pip install playwright && playwright install chromium"
                    ),
                )

        with open(tmp_path, "rb") as f:
            pdf_bytes = f.read()

        # Catch blank PDF — happens when page needs auth or didn't render
        if len(pdf_bytes) == 0:
            raise HTTPException(
                status_code=500,
                detail=(
                    "PDF generated but is empty. "
                    "The page may require authentication or took too long to render. "
                    "Try increasing wait_seconds in your request."
                ),
            )

        filename = request.filename
        if not filename.endswith(".pdf"):
            filename += ".pdf"

        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(pdf_bytes)),
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Conversion failed: {str(e)}")
    finally:
        # Always clean up the temp file
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
