from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, HttpUrl
import io
import tempfile
import os
import sys

# On Windows, ensure asyncio uses the Proactor event loop which supports subprocesses
if sys.platform == "win32":
    import asyncio
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except AttributeError:
        pass

app = FastAPI(title="URL to PDF Converter API", version="1.0.0")

# CORS — allow Angular frontend (localhost:4200)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ConvertRequest(BaseModel):
    url: HttpUrl
    filename: str = "converted.pdf"


@app.get("/")
def root():
    return {"message": "URL to PDF Converter API is running ✅"}


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.post("/convert")
async def convert_url_to_pdf(request: ConvertRequest):
    """Convert a URL to PDF and return as a downloadable file."""
    url = str(request.url)

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
                    page.goto(url, wait_until="networkidle", timeout=30000)
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

