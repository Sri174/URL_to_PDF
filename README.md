# URL to PDF Converter — FastAPI Backend

## Prerequisites (Windows)
- Python 3.10 or higher → https://python.org/downloads
- During install: ✅ check "Add Python to PATH"

---

## Setup (First Time Only)

### Option A — Using the batch scripts (easiest)
```
Double-click setup.bat
```

### Option B — Manual steps in Command Prompt
```cmd
cd url-to-pdf-api

python -m venv venv
venv\Scripts\activate

pip install -r requirements.txt
playwright install chromium
```

---

## Start the Server

### Option A — Batch script
```
Double-click start.bat
```

### Option B — Manual
```cmd
venv\Scripts\activate
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Server will be available at:
- API: http://localhost:8000
- Swagger Docs: http://localhost:8000/docs

---

## API Usage

### POST /convert
Convert a URL to a downloadable PDF.

**Request body (JSON):**
```json
{
  "url": "https://example.com",
  "filename": "my-page.pdf"
}
```

**Response:** PDF file download

---

## Angular Integration Example

```typescript
// pdf.service.ts
convertUrl(url: string, filename: string = 'page.pdf') {
  return this.http.post('http://localhost:8000/convert',
    { url, filename },
    { responseType: 'blob' }
  ).subscribe(blob => {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
  });
}
```

---

## PDF Engine Used
- **Playwright** (default) — full JS rendering via headless Chromium
- **WeasyPrint** (fallback) — pure Python, no browser needed

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `python not found` | Install Python and check "Add to PATH" |
| `playwright install` fails | Run Command Prompt as Administrator |
| Port 8000 in use | Change `--port 8000` to `--port 8001` in start.bat |
| CORS error in Angular | Make sure Angular runs on `localhost:4200` |
