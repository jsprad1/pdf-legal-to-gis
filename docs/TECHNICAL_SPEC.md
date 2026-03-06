# PDF Legal Description to GIS Boundary Converter — Technical Specification

## 1. Purpose

A web application and CLI tool that converts PDF documents containing legal property descriptions (ordinances, deeds, plats) into GIS boundary files (GeoJSON, Shapefile) with an interactive map preview.

**Input:** A PDF file containing legal descriptions of property boundaries.
**Output:** GeoJSON, Shapefile (.shp/.shx/.dbf/.prj zipped), and an HTML map viewer.

---

## 2. Architecture

```
                    +------------------+
                    |   PDF Document   |
                    +--------+---------+
                             |
                    +--------v---------+
                    | Text Extraction   |
                    | (PyMuPDF + Gemini |
                    |  OCR fallback)    |
                    +--------+---------+
                             |
                    +--------v---------+
                    | Parcel Splitter   |
                    | (regex detection  |
                    |  of parcel blocks)|
                    +--------+---------+
                             |
              +--------------+--------------+
              |                             |
    +---------v----------+     +------------v-----------+
    | Aliquot Parser     |     | Metes & Bounds Parser  |
    | (quarter-section   |     | (bearing/distance      |
    |  subdivision)      |     |  traverse engine)      |
    +---------+----------+     +------------+-----------+
              |                             |
              +--------------+--------------+
                             |
                    +--------v---------+
                    | GeoJSON Assembly  |
                    | + Shapefile Export |
                    | + Map Viewer HTML |
                    +------------------+
```

### 2.1 Entry Points

| Mode | Command | Description |
|------|---------|-------------|
| **CLI single** | `python pdf_to_gis_app.py file.pdf` | Process one PDF, output to `pdf_to_gis_jobs/<slug>/` |
| **CLI batch** | `python pdf_to_gis_app.py --batch` | Process all PDFs in `ordinances/inbox/`, move to `ordinances/processed/` |
| **Web server** | `python pdf_to_gis_app.py` | FastAPI server on port 8005 with upload UI |

### 2.2 File Structure

```
cdds/
+-- pdf_to_gis_app.py           # Main application (parsing + server)
+-- validate_legs.py            # LLM validation of parsed legs
+-- index.html                   # Web UI (Leaflet map + upload)
+-- slc_plss_sections.geojson    # PLSS section grid (reference data)
+-- requirements.txt             # Python dependencies
+-- .env                         # GEMINI_API_KEY for OCR + validation
+-- ordinances/
|   +-- inbox/                   # Drop PDFs here for batch processing
|   +-- processed/               # PDFs move here after processing
+-- pdf_to_gis_jobs/             # Output directory (one subfolder per job)
|   +-- <slug>/
|       +-- boundary.geojson
|       +-- boundary.shp/shx/dbf/prj
|       +-- shapefile.zip
|       +-- view_map.html
|       +-- legs.json              # Structured leg records
|       +-- legs_validated.json    # LLM validation results
+-- docs/
    +-- TECHNICAL_SPEC.md        # This file
```

---

## 3. Processing Pipeline — Detailed

### 3.1 Text Extraction (`extract_text_from_pdf`)

1. Open PDF with PyMuPDF (`fitz`)
2. For each page, call `page.get_text()`
3. If a page returns < 20 characters (scanned image), fall back to Gemini Vision API:
   - Render page at 300 DPI as PNG
   - Send to `gemini-2.0-flash` with a prompt to transcribe legal description text exactly
   - Gemini preserves degree symbols, bearing notation, and distances
4. Concatenate all page text

**Known character issues:** PDF extraction may render degree symbols as `~` or `?` — the bearing parser handles `DEGREES`, `DEG.`, `deg`, `~`, and the replacement character.

### 3.2 Document Name Extraction (`extract_document_name`)

Attempts to find a meaningful name from the text:
- Looks for "THE ___ COMMUNITY DEVELOPMENT DISTRICT" pattern
- Falls back to the PDF filename (cleaned up)

### 3.3 Parcel Splitting (`split_into_parcels`)

Identifies individual parcel descriptions within the full text. Two parcel header formats:

| Format | Example | Regex Pattern |
|--------|---------|---------------|
| Paren/brace | `("PARCEL 4")` | `[({\"']PARCEL\s+(\w+)[)}]+` |
| Colon | `Parcel B:` | `PARCEL\s+(\w+)\s*:` |

Also detects standalone metes-and-bounds blocks: `"A PORTION OF LAND...POINT OF BEGINNING."`

Each parcel is classified:
- **Has `COMMENCE`/`COMMENCING`** -> metes_and_bounds
- **Has fraction notation (1/4, 1/2)** -> aliquot
- **Has `LESS AND EXCEPT`** -> sub-parcels created for exclusion zones

### 3.4 Aliquot Part Processing

For quarter-section descriptions like "THE SOUTH 1/2 OF THE NORTHEAST 1/4 OF THE SOUTHEAST 1/4 OF SECTION 1, TOWNSHIP 35 SOUTH, RANGE 39 EAST":

1. **Parse section reference** — extract (Township, Range, Section) from text
2. **Look up PLSS section** — get bounding box from `slc_plss_sections.geojson`
3. **Parse subdivision chain** — extract directional tokens: `['S', 'NE', 'SE']`
4. **Apply subdivisions right-to-left** — SE quarter first, then NE quarter of that, then S half of that
5. **Handle trim clauses** — "SOUTH 13 FEET OF" or "LESS AND EXCEPT THE WEST 52.50 FEET"
6. **Output** — rectangular polygon from the resulting bounding box

**PLSS Data:** `slc_plss_sections.geojson` contains section boundaries for St. Lucie County, FL (State Plane FL East, NAD83). Each section is indexed by `(TWP, RGE, SECNO)` tuple. Corner coordinates (NW, NE, SW, SE, C) are derived from the section polygon's bounding box.

### 3.5 Metes-and-Bounds Processing

For bearing/distance traverse descriptions. This is the complex path.

#### 3.5.1 Finding the Starting Point (`find_commencing_point`)

Matches: `"COMMENCING AT THE SOUTHEAST CORNER OF SECTION 6, TOWNSHIP 34 SOUTH, RANGE 40 EAST"`

Returns: `(corner, township, range, section)` -> looks up the PLSS corner coordinates.

Two patterns supported:
- Full reference (corner + section + township + range in one clause)
- Partial reference (corner + section, with township/range found elsewhere in text)

#### 3.5.2 Commencing vs. Boundary

The text is split on "POINT OF BEGINNING":
- **Commencing courses** (before POB) — traverse from the PLSS corner to the actual start point
- **Boundary courses** (after POB) — the actual property boundary that forms the polygon

#### 3.5.3 Course Parsing (`parse_courses_from_text`)

Text is split on `THENCE` to identify individual courses. Each course is classified:

**Line course:**
- Bearing: `N 89 59'21" W` -> azimuth 270.01 (0-360 clockwise from north)
- Distance: `1,037.83 FEET`

**Curve course:**
- Radius: `RADIUS OF 550.00 FEET`
- Arc length: `ARC DISTANCE OF 464.57 FEET`
- Concave direction: `CONCAVE SOUTHWESTERLY` (determines left/right turn)
- Chord bearing (optional): `CHORD BEARING OF N 56 45'26" W`
- Central angle (informational, derived from radius + arc)

**Line-to-boundary course** (bearing only, no distance):
- Used for closing legs that terminate at a section line or back to POB

#### 3.5.4 Bearing Parser (`parse_bearing`)

Converts surveyor quadrant bearings to azimuths:

| Quadrant | Formula | Example |
|----------|---------|---------|
| N __ E | angle | N 45 00'00" E -> 45.0 |
| S __ E | 180 - angle | S 45 00'00" E -> 135.0 |
| S __ W | 180 + angle | S 45 00'00" W -> 225.0 |
| N __ W | 360 - angle | N 45 00'00" W -> 315.0 |

Handles degree symbol variants: `DEGREES`, `DEG.`, `deg`, `~`, `?`

#### 3.5.5 Geodetic Traverse Engine (`GeodeticTraverse`)

Uses `pyproj.Geod(ellps="WGS84")` for ellipsoidal computations.

**`advance(lat, lon, azimuth, distance_ft)`**
- Converts feet to meters
- Calls `Geod.fwd()` to compute destination point
- Updates `tangent_bearing` for subsequent courses
- Returns `(new_lat, new_lon)`

**`advance_curve(lat, lon, radius_ft, arc_ft, direction, chord_bearing, concave_dir)`**

This is the most complex operation. The algorithm:

1. **Compute delta angle:** `delta = arc_length / radius` (radians)
2. **Determine left/right from concave direction:**
   - Convert concave keyword to approximate azimuth (N=0, NE=45, E=90, etc.)
   - Compute right-radial: `tangent_bearing + 90`
   - Compute left-radial: `tangent_bearing - 90`
   - Whichever is closer to the concave azimuth determines the turn direction
3. **Compute chord bearing** (if not provided): `chord = tangent + sign * delta/2`
4. **Interpolate arc points** (not just a single chord):
   - Find arc center point: perpendicular to tangent, at radius distance
   - Step around the center at regular angular intervals
   - Generate 1 point per ~2 degrees of arc, minimum 8 points
5. **Update tangent bearing** for next course: `new_tangent = chord + sign * delta/2`

**Key relationships:**
```
delta = arc_length / radius
chord_length = 2 * R * sin(delta / 2)
chord_bearing = incoming_tangent +/- (delta / 2)
outgoing_tangent = incoming_tangent +/- delta
outgoing_tangent = chord_bearing +/- (delta / 2)
```

**`apply_compass_rule(points, target_lat, target_lon)`**

Distributes closure error proportionally along the traverse (Bowditch/Compass rule). Applied after all boundary courses to close the polygon back to the POB.

### 3.5.7 Gap Inference Engine (`resolve_gaps`)

**This is a critical feature.** Real-world legal descriptions from scanned PDFs often have OCR artifacts that break standard parsing — split words ("Sout h"), mangled symbols, newlines mid-bearing, or informal phrasing ("continue Southeasterly"). Without gap inference, these unparseable fragments are silently dropped, causing large closure errors.

The gap inference engine runs two passes after initial course parsing:

**Pass 1 — Relaxed Parsing** (`_try_relaxed_parse`):
1. Clean OCR artifacts: merge split short words, remove newlines, deduplicate quote marks
2. Try standard bearing+distance parse on cleaned text
3. If that fails, extract directional words + distance (e.g., "continue Southeasterly... 29.87 feet" → azimuth 135°, distance 29.87 ft)
4. If that fails, try curve detection on cleaned text

**Pass 2 — Geometric Inference** (`_infer_gap_from_neighbors`):
For remaining gaps, use surrounding legs like puzzle pieces:
1. Run the traverse forward up to the gap → know where it starts (point A)
2. Look at where the next good leg picks up → know where it ends (point B)
3. Compute bearing and distance from A to B
4. Insert a straight-line leg to fill the gap

**Confidence levels:**
- `relaxed_parse`: High — real parse from cleaned text
- `directional_word`: Medium — approximate direction (8-point compass)
- `geometric`: Low — assumes straight line between known endpoints

**Real-world impact (Seagrove Parcel B, 65-leg river boundary):**
- Without gap inference: 5 dropped legs, 240 ft closure error
- With gap inference: 0 dropped legs, 107 ft closure error (55% improvement)

Each inferred leg is marked in the legs table with `"inferred": "<method>"` and reduced confidence (0.5 vs 1.0) so downstream validation can flag them.

### 3.6 Output Generation

**GeoJSON:** Standard FeatureCollection with properties: `name`, `parcel`, `desc_type`, `section`.

**Shapefile:** Created with `pyshp`. Fields: NAME, PARCEL, DESC_TYPE, SECTION. Projection file (.prj) uses WGS84.

**Map Viewer:** Standalone HTML file with Leaflet.js showing all parcels color-coded with popups.

---

## 4. Web UI

Minimal full-screen map interface:
- Header bar with Upload button, status text, and download buttons (GeoJSON, Shapefile)
- Full-screen Leaflet map
- Drag-and-drop PDF upload
- Polls `/api/status/<job_id>` every second until processing completes
- Displays result polygons on map with color coding

### 4.1 API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Serves `index.html` |
| `/api/upload` | POST | Upload PDF, returns `{job_id}` |
| `/api/status/<job_id>` | GET | Returns `{status, geojson?, error?}` |
| `/api/download/<job_id>/geojson` | GET | Download boundary.geojson |
| `/api/download/<job_id>/shp` | GET | Download shapefile.zip |

---

## 5. Known Limitations and Future Work

### 5.1 Current Limitations

- **PLSS data is St. Lucie County only** — `slc_plss_sections.geojson` covers SLC sections. To work in other counties/states, additional PLSS data would be needed.
- **Aliquot parcels are rectangular approximations** — they use bounding box subdivision, which doesn't account for irregular section shapes.
- **Line-to-boundary courses** use heuristic distance estimation — they try to compute distance to the nearest section subdivision line.
- **Compound parcel merging** — multiple aliquot sub-descriptions within one parcel are merged by overall bounding box, which can overcount area if they're non-contiguous.
- **No spiral/transition curves** — only circular arcs are supported.

### 5.2 OCR Limitations

- Gemini OCR requires a valid `GEMINI_API_KEY` in `.env`
- OCR quality depends on scan quality — poor scans may produce garbled bearings
- Each empty page triggers an API call (cost consideration for large documents)

### 5.3 Future Enhancements

- **Supabase integration** — store jobs, PDFs, and outputs in cloud storage/database
- **Vercel deployment** — host the web UI publicly
- **Additional PLSS datasets** — extend beyond St. Lucie County
- **Parcel validation** — area vs. stated acreage, overlap detection
- **Batch progress reporting** — webhook or SSE for processing status

---

## 6. LLM Validation Pipeline (`validate_legs.py`)

After the regex-based parser extracts traverse legs, an LLM validation pass cross-checks the results.

### 6.1 Legs Table

Each metes-and-bounds parcel produces a structured "legs table" — a list of leg records:

| Field | Type | Description |
|-------|------|-------------|
| `leg_num` | int | Sequential leg number (0-based) |
| `phase` | str | `"commencing"` or `"boundary"` |
| `type` | str | `"line"`, `"curve"`, or `"line_to_boundary"` |
| `bearing_raw` | str | Original surveyor bearing text |
| `azimuth` | float | Computed azimuth (0-360 clockwise from north) |
| `distance_ft` | float | Distance in feet (lines) |
| `radius_ft` | float | Curve radius in feet |
| `arc_ft` | float | Arc length in feet |
| `concave_dir` | str | Concave direction keyword (curves) |
| `start_lat/lon` | float | Starting coordinate |
| `end_lat/lon` | float | Ending coordinate |

Saved as `legs.json` in each job's output directory, keyed by parcel name.

### 6.2 Validation Process

1. Format legs into a readable table
2. Send to **Gemini 2.0 Flash** with the raw legal description text
3. LLM checks for:
   - **Gaps**: coordinate discontinuities between consecutive legs
   - **Missing legs**: count mismatch vs. stated number of courses
   - **Bearing reversals**: direction contradictions with source text
   - **Number parsing errors**: bearing/distance/radius mismatches
   - **Closure error assessment**: good (<5 ft), marginal (5-50 ft), poor (>50 ft)
4. Each leg receives a `confidence` score (0.0-1.0) and optional `flags` list
5. Results saved as `legs_validated.json`

### 6.3 Integration Points

- **CLI mode**: Validation runs after leg extraction, results printed to console
- **Web server**: Validation runs in background task, results saved to job directory
- **Batch mode**: Validation runs per-parcel for each PDF
- **Standalone**: `python validate_legs.py <legs.json> [raw_text.txt]`

---

## 7. Dependencies

| Package | Purpose |
|---------|---------|
| `PyMuPDF` (fitz) | PDF text extraction + page rendering |
| `pyproj` | Geodetic computations (Geod.fwd, Geod.inv) |
| `pyshp` | Shapefile creation |
| `FastAPI` | Web server framework |
| `uvicorn` | ASGI server |
| `python-multipart` | File upload handling |
| `google-genai` | Gemini Vision OCR + LLM validation |
| `python-dotenv` | Load API keys from .env |

---

## 8. Data Flow Examples

### 8.1 Aliquot: "S 1/2 of NE 1/4 of SE 1/4, Section 1, T35S R39E"

```
1. Parse section ref: (35, 39, 1)
2. Look up section bbox: {min_lat: 27.454, max_lat: 27.470, min_lon: -80.391, max_lon: -80.383}
3. Parse chain: ['S', 'NE', 'SE']
4. Apply reversed: SE -> subdivide to SE quarter
                   NE -> subdivide to NE quarter of that
                   S  -> subdivide to S half of that
5. Result: rectangular polygon ~660ft x 1320ft
```

### 8.2 Metes-and-Bounds Traverse

```
1. Find commencing point: "SE corner of Section 6, T34S R40E"
   -> PLSS lookup: (27.5482, -80.3678)

2. Run commencing courses (to find POB):
   N 89 41'27" W, 2389.13 ft -> advance to intermediate point
   N 18 40'57" W, 2268.13 ft -> advance along road ROW
   S 89 44'41" E, 1107.12 ft -> arrive at POB

3. Run boundary courses:
   N 19 32'45" W, 620.44 ft -> straight line
   S 89 49'18" E, 781.79 ft -> straight line
   [64 courses along Indian River] -> bearing+distance pairs
   Curve concave South, R=550, arc=464.57 -> interpolate 24 arc points
   Close to POB

4. Apply compass rule to distribute closure error
5. Output polygon with ~66 vertices
```

---

## 9. Testing

Test PDFs are in `ordinances/`:

| PDF | Type | Expected Parcels | Key Features |
|-----|------|-----------------|--------------|
| Eagle Bend SLC 24-004.pdf | Text | 11 | Aliquot + metes-and-bounds + exclusions + curve |
| Seagrove SLC 23-016.pdf | Text | 5 | 64-course river boundary + 2 curves in Parcel F |
| Pineapple Grove SLC 25-018.pdf | Text | TBD | Needs testing |
| Sundance PSL 24-26.pdf | **Scanned** | TBD | Requires Gemini OCR |
| Sunrise FP 24-003.PDF | Mixed | TBD | Some pages need OCR |
| Symphony Lakes FP 25-006.pdf | Mixed | TBD | Many pages need OCR |
| Veranda II PSL 18-30.pdf | **Scanned** | TBD | Requires Gemini OCR |

### Validation Approach

1. Compare output polygon centroids against known parcel locations in county GIS
2. Check closure error (distance from last point back to POB)
3. Compare computed acreage against stated acreage in the ordinance
4. Visual inspection of generated map overlaid on satellite imagery
