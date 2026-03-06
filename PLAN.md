# CDDS Next Session Plan

## Goal
Build a human-in-the-loop review UI so scanned PDFs produce accurate boundaries.

## Problem
Scanned PDF OCR is non-deterministic — each run produces slightly different text.
One wrong digit in a bearing (e.g., S39° vs S89°) throws the whole shape off by thousands of feet.
The Sundance PDF (15 pages, all scanned) consistently produces 2000-6000 ft closure errors.

## Solution: Human-in-the-Loop Review UI

### How it works
1. User uploads PDF
2. App extracts courses (regex first, Gemini vision fallback for scanned docs)
3. Instead of immediately generating the final shape, show a **review table**:
   - Each row = one leg/course
   - Columns: leg #, bearing, distance, radius/arc (if curve), confidence score
   - Next to each row: thumbnail of the source PDF page region where that leg was read
   - Color coding: green = high confidence, yellow = medium, red = low/gap
4. User can click any cell to edit the value (fix wrong bearings/distances)
5. Live map preview updates as user edits values
6. When satisfied, user clicks "Generate" to produce final GeoJSON/Shapefile

### Architecture
```
Upload PDF
    |
    v
[Extract + Parse] -- regex + Gemini vision page-by-page
    |
    v
[Review UI] -- table of legs with source page images
    |            user corrects any errors
    |            live map preview updates
    v
[Generate Output] -- GeoJSON, Shapefile, map
```

### Implementation Tasks

#### 1. Cleanup the project
- [ ] Delete `pdf_to_gis_jobs/` contents (old job output)
- [ ] Delete `__pycache__/`
- [ ] Keep only: `ordinances/inbox/`, `ordinances/processed/`
- [ ] Remove `.claude/agents/` (not using multi-agent for this)
- [ ] Ensure `.gitignore` covers: `.env`, `__pycache__/`, `pdf_to_gis_jobs/`, `ordinances/`, `.vercel`
- [ ] Push clean state to GitHub

#### 2. New API endpoint: `/api/review`
- Returns parsed legs with confidence scores and page references
- Each leg includes: `page_num`, `bbox` (region on page where text was found)
- Response format:
```json
{
  "job_id": "abc123",
  "doc_name": "Sundance CDD",
  "section_ref": "T35S R39E Sec 31",
  "start_corner": "SW",
  "legs": [
    {
      "leg_num": 0,
      "phase": "commencing",
      "type": "line",
      "bearing": "N00°03'37\"W",
      "azimuth": 359.94,
      "distance_ft": 1101.96,
      "confidence": 1.0,
      "source_page": 6,
      "raw_text": "N00°03'37\"W, A DISTANCE OF 1,101.96 FEET"
    }
  ],
  "page_images": ["/api/page-image/abc123/6", "/api/page-image/abc123/7", ...]
}
```

#### 3. New API endpoint: `/api/page-image/{job_id}/{page_num}`
- Returns a rendered PNG of the specified PDF page
- Used by the review UI to show source context

#### 4. New API endpoint: `/api/generate`
- Accepts the edited legs array from the review UI
- Runs the geodetic traverse with the corrected values
- Returns GeoJSON + shapefile

#### 5. Update `index.html` — Review UI
- After upload, show a split view:
  - LEFT: scrollable table of legs (editable cells)
  - RIGHT: map preview + PDF page viewer
- Clicking a leg highlights it on the map and scrolls to the source page
- Editing a bearing/distance re-runs the traverse and updates the map live
- "Generate" button finalizes and enables GeoJSON/Shapefile download
- Color-coded confidence: green (>0.9), yellow (0.5-0.9), red (<0.5)

#### 6. Improve extraction pipeline
- Save OCR text to disk after first extraction (deterministic re-runs)
- Page-by-page Gemini vision extraction for scanned docs (already implemented)
- Deduplicate courses at page boundaries
- Use `gemini-2.5-pro` for structured extraction, `gemini-2.5-flash` for OCR/classification

### What's Already Done
- Regex improvements: apostrophe bearings, O→0 substitution, run-on numbers, cardinal directions
- Page-by-page vision extraction with page classification
- Gemini 2.5 model upgrade (from 2.0)
- Vercel deployment at https://cdds.vercel.app (needs redeploy after changes)
- GitHub repo at https://github.com/jsprad1/pdf-legal-to-gis
- Duplicate PDF processing bug fixed

### File Structure After Cleanup
```
cdds/
├── CLAUDE.md              # Project instructions
├── PLAN.md                # This file (delete after implemented)
├── .env                   # API keys
├── .env.example
├── .gitignore
├── requirements.txt
├── pdf_to_gis_app.py      # Main app (backend + CLI)
├── index.html             # Web UI (will be rewritten for review UI)
├── vercel.json            # Vercel deployment config
├── api/index.py           # Vercel serverless adapter
├── slc_plss_sections.geojson  # PLSS reference grid
├── validate_legs.py       # Leg validation
├── docs/TECHNICAL_SPEC.md
├── ordinances/
│   ├── inbox/             # Drop PDFs here
│   └── processed/         # PDFs move here after processing
└── pdf_to_gis_jobs/       # Processing output (gitignored)
```
