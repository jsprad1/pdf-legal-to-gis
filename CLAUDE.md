# PDF Legal Description to GIS Boundary Converter

## Purpose
Web app (and CLI) that takes a PDF containing legal descriptions (ordinances, deeds, plats)
and outputs GIS boundary files (GeoJSON, Shapefile) with a map preview.

**Workflow:** Upload PDF → extract legal description → parse boundaries → download GeoJSON/Shapefile

## How It Works
1. PDF text extraction via PyMuPDF (Gemini API fallback for image-only pages)
2. Parses two types of legal descriptions:
   - **Aliquot** (quarter-section): e.g. "S 1/2 of NE 1/4 of SE 1/4, Section 1, T35S R39E"
   - **Metes-and-bounds**: bearing/distance traversals from a Point of Beginning
3. Uses PLSS section grid for geographic reference (FL State Plane East, NAD83)
4. Outputs GeoJSON + Shapefile + interactive Leaflet map preview

## Key Data
- **PLSS grid**: `slc_plss_sections.geojson` — section corner coordinates for St. Lucie County
- **Gemini API key**: `.env` contains `GEMINI_API_KEY` for OCR fallback

## Environment
- Python 3.x with PyMuPDF, pyproj, pyshp
- FastAPI + Uvicorn for web server (port 8005)
- Leaflet.js for map rendering in frontend

## File Structure
```
├── CLAUDE.md                 # This file
├── .env                      # API keys (GEMINI_API_KEY)
├── pdf_to_gis_app.py         # Main app (web server + CLI)
├── index.html                # Web UI
├── slc_plss_sections.geojson # PLSS section reference grid
├── requirements.txt          # Python dependencies
├── ordinances/               # Sample PDF source documents
│   ├── inbox/                # Drop PDFs here for batch processing
│   └── processed/            # PDFs move here after batch processing
├── pdf_to_gis_jobs/          # Processing output (per-job directories)
└── docs/
    └── TECHNICAL_SPEC.md     # Detailed technical specification
```

## Technical Spec
See `docs/TECHNICAL_SPEC.md` for the full technical specification including:
- Processing pipeline details (text extraction, parcel detection, traverse engine)
- Curve computation algorithm (concave direction, arc interpolation, tangent tracking)
- API endpoints, data flow examples, testing matrix
- Known limitations and future work

## Usage
```bash
# Web server
python pdf_to_gis_app.py

# CLI
python pdf_to_gis_app.py path/to/document.pdf
```
