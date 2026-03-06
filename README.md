# PDF Legal Description to GIS Boundary Converter

Converts PDF documents containing legal property descriptions (ordinances, deeds, plats) into GIS boundary files (GeoJSON, Shapefile) with an interactive map preview.

**Upload PDF -> Extract legal description -> Parse boundaries -> Download GeoJSON/Shapefile**

Handles two types of legal descriptions:
- **Aliquot** (quarter-section): e.g. "S 1/2 of NE 1/4 of SE 1/4, Section 1, T35S R39E"
- **Metes-and-bounds**: bearing/distance traversals from a Point of Beginning

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and edit your environment file
cp .env.example .env
# Add your Gemini API key (needed for scanned/image PDFs only)

# Run web server
python pdf_to_gis_app.py
# Open http://localhost:8005
```

## Usage

### Web UI
Run `python pdf_to_gis_app.py` and open http://localhost:8005. Drag and drop a PDF or click Upload. Results appear on the map and are available for download as GeoJSON or Shapefile.

### CLI
```bash
# Process a single PDF
python pdf_to_gis_app.py path/to/document.pdf

# Batch process all PDFs in ordinances/inbox/
python pdf_to_gis_app.py --batch
```

Output goes to `pdf_to_gis_jobs/<slug>/` with:
- `boundary.geojson` — import into QGIS, Google Earth, etc.
- `shapefile.zip` — import into ArcGIS
- `view_map.html` — open in browser for a quick preview

## Requirements

- Python 3.9+
- Dependencies: `pip install -r requirements.txt`
- `slc_plss_sections.geojson` — PLSS section grid (included, St. Lucie County FL)
- Gemini API key (optional, only needed for scanned/image-only PDFs)

## How It Works

1. PDF text extraction via PyMuPDF (Gemini API fallback for image-only pages)
2. Regex-based parsing of legal descriptions (aliquot chains, bearing/distance courses, curves)
3. Geodetic traverse computation using WGS84 ellipsoid (pyproj)
4. PLSS section grid lookup for geographic reference
5. Output as GeoJSON + Shapefile + interactive Leaflet map
