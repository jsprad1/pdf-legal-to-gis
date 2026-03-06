"""Vercel serverless adapter for PDF Legal Description → GIS Boundary Converter."""

import json
import sys
import tempfile
import zipfile
import io
import base64
from pathlib import Path

# Add parent dir so we can import the main app module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI()

# Lazy-load the heavy processing module (cold start optimization)
_engine = None

def get_engine():
    global _engine
    if _engine is None:
        import pdf_to_gis_app as engine
        _engine = engine
    return _engine


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).resolve().parent.parent / "index.html"
    if html_path.exists():
        return html_path.read_text()
    return "<h1>PDF-to-GIS Boundary Converter</h1>"


@app.post("/api/upload")
async def upload_pdf(file: UploadFile = File(...)):
    """Process PDF synchronously and return results directly."""
    contents = await file.read()

    MAX_MB = 50
    if len(contents) > MAX_MB * 1024 * 1024:
        return JSONResponse({"error": f"File too large (max {MAX_MB}MB)"}, status_code=413)

    # Write to temp file for processing
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(contents)
        tmp_path = Path(tmp.name)

    try:
        engine = get_engine()
        geojson = engine.process_ordinance(tmp_path, verbose=False)

        # Clean internal fields from features
        for feat in geojson.get("features", []):
            feat.pop("_legs", None)
            feat.pop("_raw_text", None)

        # Build shapefile zip in memory
        shp_b64 = None
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                prefix = Path(tmpdir) / "boundary"
                engine.create_shapefile(geojson, prefix)
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w") as z:
                    for ext in [".shp", ".shx", ".dbf", ".prj"]:
                        f = Path(f"{prefix}{ext}")
                        if f.exists():
                            z.write(f, f"boundary{ext}")
                shp_b64 = base64.b64encode(buf.getvalue()).decode()
        except Exception:
            pass  # Shapefile generation is best-effort

        return {
            "status": "completed",
            "geojson": geojson,
            "shapefile_zip_b64": shp_b64,
        }

    except Exception as e:
        return JSONResponse({"status": "failed", "error": str(e)}, status_code=500)

    finally:
        tmp_path.unlink(missing_ok=True)
