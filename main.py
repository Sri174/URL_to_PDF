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

# On Windows, ensure asyncio uses the Proactor event loop which supports subprocesses
if sys.platform == "win32":
    import asyncio
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except AttributeError:
        pass

app = FastAPI(title="URL to PDF Converter API", version="1.0.0")

# Runtime configuration
RUN_MODE = os.getenv("RUN_MODE", "render").lower()  # 'local' or 'render'
ANGULAR_URL = os.getenv("ANGULAR_URL", "http://localhost:4200")

# CORS — allow Angular frontend (localhost:4200) and production Angular URL
cors_origins = ["http://localhost:4200"]
if ANGULAR_URL:
    cors_origins.append(ANGULAR_URL)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ConvertRequest(BaseModel):
    url: str
    filename: str = "converted.pdf"


def _is_private_hostname(hostname: str) -> bool:
    """Return True if hostname resolves to a private or non-routable IP."""
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
    """Try to fetch the URL to verify reachability from this host."""
    try:
        resp = requests.get(url, timeout=timeout, allow_redirects=True)
        return resp.status_code < 400
    except Exception:
        return False


@app.get("/")
def root():
    return {"message": "URL to PDF Converter API is running ✅"}


@app.get("/health")
def health_check():
    return {"status": "ok", "run_mode": RUN_MODE}


@app.post("/convert")
async def convert_url_to_pdf(request: ConvertRequest):
    """Convert a URL to PDF and return as a downloadable file."""
    url = request.url.strip()

    # Basic URL parsing
    parsed = urlparse(url)
    if not parsed.scheme:
        raise HTTPException(status_code=400, detail="URL must include a scheme (http:// or https://)")

    hostname = parsed.hostname or ""

    # RUN_MODE specific validations
    if RUN_MODE == "local":
        # In local mode we allow any URL (including local/private addresses)
        pass
    else:
        # RENDER mode: block private hostnames and verify reachability
        if _is_private_hostname(hostname):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Refused: detected a private or non-routable host. "
                    "This deployment is running in RENDER mode and cannot access local network addresses. "
                    "Run the service locally (set RUN_MODE=local) to convert local URLs."
                ),
            )

        if not _is_publicly_accessible(url, timeout=10):
            raise HTTPException(
                status_code=400,
                detail=(
                    "URL is not publicly reachable from this server. "
                    "Ensure the URL is accessible from the public internet or run the converter locally."
                ),
            )

    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_file:
            tmp_path = tmp_file.name

        # --- Strategy 1: Playwright (best quality, full JS rendering) ---
        try:
            import asyncio
            from playwright.sync_api import sync_playwright

            def _convert_with_playwright():
                with sync_playwright() as p:
                    browser = p.chromium.launch()
                    page = browser.new_page()
                    # Increased timeout to 60s to allow slower pages to load
                    page.goto(url, wait_until="networkidle", timeout=60000)
                    page.pdf(path=tmp_path, format="A4", print_background=True)
                    browser.close()

            await asyncio.to_thread(_convert_with_playwright)

        except ImportError:
            # --- Strategy 2: WeasyPrint (pure Python, no browser needed) ---
            try:
                import weasyprint
                weasyprint.HTML(url=url).write_pdf(tmp_path)
            except ImportError:
                raise HTTPException(
                    status_code=500,
                    detail=(
                        "No PDF engine found. "
                        "Run: pip install playwright && playwright install chromium  "
                        "OR: pip install weasyprint"
                    ),
                )

        with open(tmp_path, "rb") as f:
            pdf_bytes = f.read()

        os.unlink(tmp_path)

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

