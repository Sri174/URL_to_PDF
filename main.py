from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional
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

app = FastAPI(title="URL to PDF Converter API", version="3.0.0")

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
    wait_seconds: float = 5.0        # wait time for Angular to render

    # Optional auth — if provided, Playwright will log in before capturing
    login_url: Optional[str] = None  # e.g. "https://emgapp.zenoinfo.ae/#/login"
    username: Optional[str] = None
    password: Optional[str] = None

    # CSS selectors for the login form fields
    username_selector: str = "input[type='text'], input[name='username'], input[id*='user'], input[placeholder*='user' i], input[placeholder*='email' i]"
    password_selector: str = "input[type='password']"
    submit_selector: str   = "button[type='submit'], input[type='submit'], button:has-text('Login'), button:has-text('Sign in')"


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
    """Check reachability using the base URL only (ignores hash fragment)."""
    try:
        parsed   = urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        resp     = requests.get(base_url, timeout=timeout, allow_redirects=True)
        return resp.status_code < 500
    except Exception:
        return False


def _convert_with_playwright(
    url: str,
    tmp_path: str,
    wait_seconds: float,
    login_url: Optional[str],
    username: Optional[str],
    password: Optional[str],
    username_selector: str,
    password_selector: str,
    submit_selector: str,
) -> None:
    """
    1. Optionally log in with username/password.
    2. Navigate to the target URL.
    3. Wait for Angular to render.
    4. Capture as PDF.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            java_script_enabled=True,
        )
        page = context.new_page()

        try:
            # ── Step 1: Login if credentials provided ────────────────────────
            if login_url and username and password:
                page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(2000)  # wait for login form to render

                # Fill username
                page.fill(username_selector, username)
                page.wait_for_timeout(500)

                # Fill password
                page.fill(password_selector, password)
                page.wait_for_timeout(500)

                # Click submit
                page.click(submit_selector)

                # Wait for navigation after login (up to 15s)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                    page.wait_for_timeout(2000)  # allow post-login redirect to settle
                except PWTimeout:
                    pass  # some SPAs don't trigger full navigation on login

            # ── Step 2: Navigate to target URL ───────────────────────────────
            # Use domcontentloaded for Angular hash routes (/#/path)
            wait_strategy = "domcontentloaded" if "#" in url else "networkidle"
            page.goto(url, wait_until=wait_strategy, timeout=60000)

            # ── Step 3: Wait for Angular to fully render ──────────────────────
            page.wait_for_timeout(int(wait_seconds * 1000))

            # Best-effort: confirm page has visible content
            try:
                page.wait_for_function(
                    "document.body && document.body.innerText.trim().length > 50",
                    timeout=15000,
                )
            except Exception:
                pass  # proceed anyway

            # Scroll to trigger any lazy-loaded content
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(500)

            # ── Step 4: Capture PDF ───────────────────────────────────────────
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
        "version": "3.0.0",
        "run_mode": RUN_MODE,
    }


@app.get("/health")
def health_check():
    return {"status": "ok", "run_mode": RUN_MODE}


@app.post("/convert")
async def convert_url_to_pdf(request: ConvertRequest):
    """Convert any URL to a downloadable PDF, with optional login support."""
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
            await asyncio.to_thread(
                _convert_with_playwright,
                url,
                tmp_path,
                request.wait_seconds,
                request.login_url,
                request.username,
                request.password,
                request.username_selector,
                request.password_selector,
                request.submit_selector,
            )
        except ImportError:
            try:
                import weasyprint
                weasyprint.HTML(url=url).write_pdf(tmp_path)
            except ImportError:
                raise HTTPException(
                    status_code=500,
                    detail="Install: pip install playwright && playwright install chromium",
                )

        with open(tmp_path, "rb") as f:
            pdf_bytes = f.read()

        # Catch blank PDF
        if len(pdf_bytes) == 0:
            raise HTTPException(
                status_code=500,
                detail=(
                    "PDF generated but is empty. "
                    "Check that login credentials are correct and selectors match the login form."
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
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
