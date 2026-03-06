#!/usr/bin/env python3
"""
PDF Legal Description → GIS Boundary Converter

Handles both aliquot (quarter-section) and metes-and-bounds legal descriptions.
Extracts text directly from PDF, parses with regex, falls back to Gemini for image-only pages.

Usage:
    python pdf_to_gis_app.py path/to/file.pdf           # Process single PDF
    python pdf_to_gis_app.py --batch                    # Process all PDFs in ordinances/inbox/
    python pdf_to_gis_app.py                            # Web server on port 8005
"""

import json, math, re, sys, uuid, zipfile, io, os, traceback, shutil, logging
from pathlib import Path
from typing import Optional, List, Tuple, Dict

import fitz  # PyMuPDF
import shapefile
from pyproj import Geod
from dotenv import load_dotenv

# ── Module-level init ────────────────────────────────────────────────────────
log = logging.getLogger(__name__)

load_dotenv(Path(__file__).parent / ".env")

# ── Config ────────────────────────────────────────────────────────────────────
HERE = Path(__file__).parent
WORK_DIR = HERE / "pdf_to_gis_jobs"
WORK_DIR.mkdir(exist_ok=True)
INBOX = HERE / "ordinances" / "inbox"
PROCESSED = HERE / "ordinances" / "processed"
INBOX.mkdir(parents=True, exist_ok=True)
PROCESSED.mkdir(parents=True, exist_ok=True)

# ── PLSS Section Data ─────────────────────────────────────────────────────────
_PLSS_FILE = HERE / "slc_plss_sections.geojson"
_PLSS_INDEX: Dict[tuple, dict] = {}


def load_plss():
    if not _PLSS_FILE.exists():
        return
    with open(_PLSS_FILE) as f:
        gj = json.load(f)
    for feat in gj["features"]:
        p = feat["properties"]
        if p.get("TRtype") != 1:
            continue
        key = (int(p["TWP"]), int(p["RGE"]), int(p["SECNO"]))
        pts = []

        def _flatten(c):
            if isinstance(c[0], list):
                for s in c:
                    _flatten(s)
            else:
                pts.append((c[1], c[0]))  # (lat, lon)

        _flatten(feat["geometry"]["coordinates"])
        lats = [pt[0] for pt in pts]
        lons = [pt[1] for pt in pts]
        _PLSS_INDEX[key] = {
            "NW": (max(lats), min(lons)),
            "NE": (max(lats), max(lons)),
            "SW": (min(lats), min(lons)),
            "SE": (min(lats), max(lons)),
            "C": ((min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2),
        }


try:
    load_plss()
except Exception as e:
    log.warning(f"Failed to load PLSS data: {e}")


# ── Bearing Parser ────────────────────────────────────────────────────────────
_CARDINAL_AZIMUTHS = {
    "NORTH": 0, "SOUTH": 180, "EAST": 90, "WEST": 270,
    "DUE NORTH": 0, "DUE SOUTH": 180, "DUE EAST": 90, "DUE WEST": 270,
    "NORTHERLY": 0, "SOUTHERLY": 180, "EASTERLY": 90, "WESTERLY": 270,
}


def parse_bearing(text: str) -> Optional[float]:
    """Convert surveyor bearing to azimuth (0-360 clockwise from north).

    Handles formats:
        N 89°59'21" W           — full DMS
        N 89°59' W              — degrees + minutes only
        NORTH 89 DEGREES 59' 21" WEST
        N 00°32'59" W           — mangled degree symbols
        DUE NORTH / NORTHERLY   — cardinal directions
    """
    # Check cardinal bearings first (DUE NORTH, NORTHERLY, etc.)
    cardinal_m = re.search(
        r"\b(DUE\s+(?:NORTH|SOUTH|EAST|WEST)|NORTHERLY|SOUTHERLY|EASTERLY|WESTERLY)\b",
        text, re.IGNORECASE,
    )
    if cardinal_m:
        key = re.sub(r"\s+", " ", cardinal_m.group(1).upper().strip())
        if key in _CARDINAL_AZIMUTHS:
            return _CARDINAL_AZIMUTHS[key]

    # Full DMS pattern: seconds are optional
    pattern = (
        r"(NORTH|SOUTH|N|S)\s*"
        r"(\d+)\s*(?:DEGREES|DEG\.?|°|~|�)\s*"
        r"(\d+)\s*(?:MINUTES|MIN\.?|[\'′])\s*"
        r"(?:(\d+(?:\.\d+)?)\s*(?:SECONDS|SEC\.?|[\"″])?\s*)?"
        r"(EAST|WEST|E|W)"
    )
    m = re.search(pattern, text, re.IGNORECASE)
    if not m:
        return None
    ns, deg, minutes, sec, ew = m.groups()
    sec = sec or "0"
    angle = float(deg) + float(minutes) / 60 + float(sec) / 3600
    ns = ns[0].upper()
    ew = ew[0].upper()

    if ns == "N" and ew == "E":
        return angle
    if ns == "S" and ew == "E":
        return 180 - angle
    if ns == "S" and ew == "W":
        return 180 + angle
    if ns == "N" and ew == "W":
        return 360 - angle
    return None


def parse_distance(text: str) -> Optional[float]:
    """Extract distance in feet from text like 'A DISTANCE OF 1,037.83 FEET'."""
    m = re.search(r"(?:DISTANCE\s+OF\s+)?([\d,]+\.?\d*)\s*(?:FEET|FT\.?)\b", text, re.IGNORECASE)
    if m:
        return float(m.group(1).replace(",", ""))
    return None


# ── Concave Direction → Left/Right ───────────────────────────────────────────
_CONCAVE_AZIMUTHS = {
    "NORTH": 0, "NORTHERLY": 0, "N": 0,
    "NORTHEAST": 45, "NORTHEASTERLY": 45, "NE": 45,
    "EAST": 90, "EASTERLY": 90, "E": 90,
    "SOUTHEAST": 135, "SOUTHEASTERLY": 135, "SE": 135,
    "SOUTH": 180, "SOUTHERLY": 180, "S": 180,
    "SOUTHWEST": 225, "SOUTHWESTERLY": 225, "SW": 225,
    "WEST": 270, "WESTERLY": 270, "W": 270,
    "NORTHWEST": 315, "NORTHWESTERLY": 315, "NW": 315,
}


def _angle_diff(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return min(d, 360 - d)


def concave_to_direction(tangent_bearing: float, concave_keyword: str) -> str:
    """Determine left/right turn from concave direction and incoming tangent bearing."""
    concave_az = _CONCAVE_AZIMUTHS.get(concave_keyword.upper().strip())
    if concave_az is None:
        return "right"
    right_radial = (tangent_bearing + 90) % 360
    left_radial = (tangent_bearing - 90) % 360
    if _angle_diff(right_radial, concave_az) <= _angle_diff(left_radial, concave_az):
        return "right"
    return "left"


# ── Geodetic Traverse Engine ──────────────────────────────────────────────────
class GeodeticTraverse:
    def __init__(self):
        self.geod = Geod(ellps="WGS84")
        self.tangent_bearing = None

    def advance(self, lat: float, lon: float, azimuth: float, distance_ft: float) -> Tuple[float, float]:
        dist_m = distance_ft * 0.3048
        new_lon, new_lat, _ = self.geod.fwd(lon, lat, azimuth, dist_m)
        self.tangent_bearing = azimuth
        return new_lat, new_lon

    def advance_curve(self, lat, lon, radius_ft, arc_ft, direction, chord_bearing=None,
                      concave_dir=None) -> List[Tuple[float, float]]:
        """Traverse a curve, returning list of interpolated (lat, lon) points along the arc."""
        r_m = radius_ft * 0.3048
        a_m = arc_ft * 0.3048
        delta_rad = a_m / r_m if r_m > 0 else 0
        delta_deg = math.degrees(delta_rad)

        # Determine left/right from concave direction if not already resolved
        if concave_dir and self.tangent_bearing is not None:
            direction = concave_to_direction(self.tangent_bearing, concave_dir)

        sign = 1 if direction.lower() == "right" else -1

        # Compute chord bearing if not provided
        if chord_bearing is not None:
            # When chord bearing is explicit, derive tangent from it
            incoming_tangent = (chord_bearing - sign * delta_deg / 2) % 360
            if self.tangent_bearing is None:
                self.tangent_bearing = incoming_tangent
        elif self.tangent_bearing is not None:
            chord_bearing = (self.tangent_bearing + sign * delta_deg / 2) % 360
        else:
            return [(lat, lon)]  # Can't compute without any bearing reference

        # Interpolate points along the arc using center-point method
        arc_points = self._interpolate_arc(lat, lon, r_m, delta_rad, sign)

        # Update tangent bearing for the next course
        if chord_bearing is not None:
            self.tangent_bearing = (chord_bearing + sign * delta_deg / 2) % 360
        elif self.tangent_bearing is not None:
            self.tangent_bearing = (self.tangent_bearing + sign * delta_deg) % 360

        return arc_points

    def _interpolate_arc(self, lat, lon, radius_m, delta_rad, sign,
                         min_points=8, max_deg_per_seg=2.0) -> List[Tuple[float, float]]:
        """Generate points along a circular arc from (lat, lon)."""
        delta_deg = math.degrees(delta_rad)
        num_segments = max(min_points, int(delta_deg / max_deg_per_seg))

        # Find the center of the arc: perpendicular to tangent bearing
        radial_bearing = (self.tangent_bearing + sign * 90) % 360
        center_lon, center_lat, _ = self.geod.fwd(lon, lat, radial_bearing, radius_m)

        # Bearing from center back to start point
        back_az, _, _ = self.geod.inv(center_lon, center_lat, lon, lat)

        points = []
        for i in range(1, num_segments + 1):
            fraction = i / num_segments
            # Step around the center: -sign because right curves go CW around center
            angle = back_az - sign * delta_deg * fraction
            pt_lon, pt_lat, _ = self.geod.fwd(center_lon, center_lat, angle, radius_m)
            points.append((pt_lat, pt_lon))

        return points

    def apply_compass_rule(self, points, target_lat, target_lon):
        if len(points) < 3:
            return points
        total_dist = 0
        cumulative = [0]
        for i in range(1, len(points)):
            _, _, dist = self.geod.inv(points[i - 1][1], points[i - 1][0], points[i][1], points[i][0])
            total_dist += dist
            cumulative.append(total_dist)

        err_lat = target_lat - points[-1][0]
        err_lon = target_lon - points[-1][1]

        adjusted = []
        for i, pt in enumerate(points):
            ratio = cumulative[i] / total_dist if total_dist > 0 else 0
            adjusted.append((pt[0] + err_lat * ratio, pt[1] + err_lon * ratio))
        return adjusted


# ── Gap Inference Engine ──────────────────────────────────────────────────────
# "Puzzle without the picture" — when a THENCE fragment can't be parsed,
# use surrounding legs to figure out what the missing piece must be.

def _try_relaxed_parse(raw_text: str) -> Optional[dict]:
    """Try harder to extract a course from a problematic fragment.

    Handles OCR artifacts like split words ('Sout h'), extra quotes,
    newlines mid-bearing, and partial information.
    """
    # Clean up common OCR artifacts
    cleaned = raw_text
    cleaned = re.sub(r"(\w)\s+(\w)", lambda m: m.group(1) + m.group(2)
                     if len(m.group(1)) <= 2 and len(m.group(2)) <= 3 else m.group(0),
                     cleaned)  # Fix split short words like "Sout h" -> "South"
    cleaned = re.sub(r"\n", " ", cleaned)  # Remove newlines
    cleaned = re.sub(r"[\'′]{2,}", "'", cleaned)  # Dedupe minute marks
    cleaned = re.sub(r"[\"″]{2,}", '"', cleaned)  # Dedupe second marks

    # Try standard parse on cleaned text
    azimuth = parse_bearing(cleaned)
    distance = parse_distance(cleaned)
    if azimuth is not None and distance is not None:
        return {"type": "line", "azimuth": azimuth, "distance": distance, "inferred": "relaxed_parse"}

    # Try to extract just a distance (directional text like "continue southeasterly... 29.87 feet")
    if distance is not None:
        # Look for directional words
        dir_m = re.search(
            r"(NORTH(?:EAST|WEST)?(?:ERLY)?|SOUTH(?:EAST|WEST)?(?:ERLY)?|EAST(?:ERLY)?|WEST(?:ERLY)?)",
            cleaned, re.IGNORECASE,
        )
        if dir_m:
            dir_key = dir_m.group(1).upper().rstrip("ERLY").rstrip("E")
            if not dir_key:
                dir_key = dir_m.group(1).upper()
            az = _CONCAVE_AZIMUTHS.get(dir_m.group(1).upper())
            if az is not None:
                return {"type": "line", "azimuth": az, "distance": distance, "inferred": "directional_word"}

    # Try curve detection on cleaned text
    curve_m = re.search(r"RADIUS\s+OF\s+([\d,]+\.?\d*)\s*(?:FEET|FT\.?)", cleaned, re.IGNORECASE)
    arc_m = re.search(r"ARC\s+(?:LENGTH\s+|DISTANCE\s+(?:OF\s+)?)?([\d,]+\.?\d*)\s*(?:FEET|FT\.?)", cleaned, re.IGNORECASE)
    if curve_m and arc_m:
        return {
            "type": "curve",
            "radius": float(curve_m.group(1).replace(",", "")),
            "arc": float(arc_m.group(1).replace(",", "")),
            "direction": "left" if re.search(r"\bLEFT\b", cleaned, re.IGNORECASE) else "right",
            "concave_dir": None,
            "chord_bearing": parse_bearing(cleaned),
            "inferred": "relaxed_parse",
        }

    return None


def _infer_gap_from_neighbors(prev_lat, prev_lon, next_lat, next_lon, geod) -> dict:
    """Compute what a missing leg must be, given its start and end points.

    Like looking at two puzzle pieces and seeing exactly what shape fits between them.
    """
    fwd_az, _, dist_m = geod.inv(prev_lon, prev_lat, next_lon, next_lat)
    az = fwd_az % 360
    dist_ft = dist_m / 0.3048
    return {"type": "line", "azimuth": az, "distance": dist_ft, "inferred": "geometric"}


def resolve_gaps(courses: List[dict], boundary_text: str, trav: 'GeodeticTraverse',
                 start_lat: float, start_lon: float) -> Tuple[List[dict], List[dict]]:
    """Two-pass gap resolution for unparseable THENCE fragments.

    Pass 1: Try relaxed parsing (fix OCR artifacts, try alternate patterns)
    Pass 2: For remaining gaps, do a forward peek — run the traverse up to the gap,
            then run it backwards from the end, and compute what the gap must be.

    Returns (resolved_courses, gap_report) where gap_report documents what was inferred.
    """
    gap_report = []

    # Pass 1: Relaxed parsing
    resolved = []
    for i, c in enumerate(courses):
        if c["type"] == "gap":
            relaxed = _try_relaxed_parse(c["raw_text"])
            if relaxed:
                gap_report.append({
                    "gap_index": i,
                    "method": relaxed.pop("inferred"),
                    "raw_text": c["raw_text"][:80],
                    "result": "resolved",
                })
                resolved.append(relaxed)
            else:
                resolved.append(c)  # Still a gap — pass 2 will handle it
        else:
            resolved.append(c)

    # Pass 2: Geometric inference for remaining gaps
    # Strategy: run the traverse forward, and when we hit a gap, peek ahead
    # to see where the next good leg picks up.
    if not any(c["type"] == "gap" for c in resolved):
        return resolved, gap_report

    # Forward traverse to compute positions at each course
    positions = [(start_lat, start_lon)]
    temp_trav = GeodeticTraverse()
    temp_trav.tangent_bearing = trav.tangent_bearing
    lat, lon = start_lat, start_lon

    for c in resolved:
        if c["type"] == "line":
            lat, lon = temp_trav.advance(lat, lon, c["azimuth"], c["distance"])
        elif c["type"] == "curve":
            pts = temp_trav.advance_curve(
                lat, lon, c["radius"], c["arc"], c["direction"],
                c.get("chord_bearing"), c.get("concave_dir"),
            )
            if pts:
                lat, lon = pts[-1]
        elif c["type"] == "line_to_boundary":
            lat, lon = temp_trav.advance(lat, lon, c["azimuth"], 100)
        # For gaps: position stays the same (we'll fill it in)
        positions.append((lat, lon))

    # Now resolve remaining gaps using surrounding positions
    final = []
    for i, c in enumerate(resolved):
        if c["type"] == "gap":
            prev_lat, prev_lon = positions[i]
            # Look ahead to find the next non-gap position
            next_idx = i + 1
            while next_idx < len(resolved) and resolved[next_idx]["type"] == "gap":
                next_idx += 1
            if next_idx < len(positions):
                next_lat, next_lon = positions[next_idx]
            else:
                next_lat, next_lon = start_lat, start_lon  # Close to POB

            inferred = _infer_gap_from_neighbors(prev_lat, prev_lon, next_lat, next_lon, temp_trav.geod)
            gap_report.append({
                "gap_index": i,
                "method": "geometric",
                "raw_text": c.get("raw_text", "")[:80],
                "inferred_azimuth": round(inferred["azimuth"], 4),
                "inferred_distance_ft": round(inferred["distance"], 2),
                "result": "inferred",
            })
            final.append(inferred)
        else:
            final.append(c)

    return final, gap_report


# ── Aliquot Part Geometry ─────────────────────────────────────────────────────
def get_section_bbox(twp: int, rge: int, sec: int) -> Optional[dict]:
    """Get bounding box for a PLSS section."""
    corners = _PLSS_INDEX.get((twp, rge, sec))
    if not corners:
        return None
    return {
        "min_lat": corners["SW"][0],
        "max_lat": corners["NW"][0],
        "min_lon": corners["SW"][1],
        "max_lon": corners["SE"][1],
    }


def subdivide_bbox(bbox: dict, direction: str) -> dict:
    """Subdivide a bounding box by direction (N, S, E, W, NE, NW, SE, SW)."""
    mid_lat = (bbox["min_lat"] + bbox["max_lat"]) / 2
    mid_lon = (bbox["min_lon"] + bbox["max_lon"]) / 2
    d = direction.upper().strip()

    subs = {
        "N":  {"min_lat": mid_lat, "max_lat": bbox["max_lat"], "min_lon": bbox["min_lon"], "max_lon": bbox["max_lon"]},
        "S":  {"min_lat": bbox["min_lat"], "max_lat": mid_lat, "min_lon": bbox["min_lon"], "max_lon": bbox["max_lon"]},
        "E":  {"min_lat": bbox["min_lat"], "max_lat": bbox["max_lat"], "min_lon": mid_lon, "max_lon": bbox["max_lon"]},
        "W":  {"min_lat": bbox["min_lat"], "max_lat": bbox["max_lat"], "min_lon": bbox["min_lon"], "max_lon": mid_lon},
        "NE": {"min_lat": mid_lat, "max_lat": bbox["max_lat"], "min_lon": mid_lon, "max_lon": bbox["max_lon"]},
        "NW": {"min_lat": mid_lat, "max_lat": bbox["max_lat"], "min_lon": bbox["min_lon"], "max_lon": mid_lon},
        "SE": {"min_lat": bbox["min_lat"], "max_lat": mid_lat, "min_lon": mid_lon, "max_lon": bbox["max_lon"]},
        "SW": {"min_lat": bbox["min_lat"], "max_lat": mid_lat, "min_lon": bbox["min_lon"], "max_lon": mid_lon},
    }
    return subs.get(d, bbox)


def bbox_to_polygon(bbox: dict) -> List[List[float]]:
    """Convert bbox to GeoJSON polygon ring [lon, lat]."""
    return [
        [bbox["min_lon"], bbox["min_lat"]],
        [bbox["max_lon"], bbox["min_lat"]],
        [bbox["max_lon"], bbox["max_lat"]],
        [bbox["min_lon"], bbox["max_lat"]],
        [bbox["min_lon"], bbox["min_lat"]],
    ]


def trim_bbox_feet(bbox: dict, side: str, feet: float) -> dict:
    """Trim bbox to only N/S feet from a given side. Uses approximate ft-to-degree."""
    geod = Geod(ellps="WGS84")
    ns_span_m = geod.inv(bbox["min_lon"], bbox["min_lat"], bbox["min_lon"], bbox["max_lat"])[2]
    ns_span_ft = ns_span_m / 0.3048
    lat_per_ft = (bbox["max_lat"] - bbox["min_lat"]) / ns_span_ft if ns_span_ft > 0 else 0

    result = dict(bbox)
    side = side.upper()
    if side == "S":
        result["max_lat"] = result["min_lat"] + lat_per_ft * feet
    elif side == "N":
        result["min_lat"] = result["max_lat"] - lat_per_ft * feet
    return result


def trim_bbox_from_side(bbox: dict, side: str, feet: float) -> dict:
    """Remove a strip of `feet` width from one side of the bbox (for LESS AND EXCEPT)."""
    geod = Geod(ellps="WGS84")
    if side in ("W", "E"):
        ew_span_m = geod.inv(bbox["min_lon"], bbox["min_lat"], bbox["max_lon"], bbox["min_lat"])[2]
        ew_span_ft = ew_span_m / 0.3048
        lon_per_ft = (bbox["max_lon"] - bbox["min_lon"]) / ew_span_ft if ew_span_ft > 0 else 0
        result = dict(bbox)
        if side == "W":
            result["min_lon"] += lon_per_ft * feet
        else:
            result["max_lon"] -= lon_per_ft * feet
        return result
    else:
        ns_span_m = geod.inv(bbox["min_lon"], bbox["min_lat"], bbox["min_lon"], bbox["max_lat"])[2]
        ns_span_ft = ns_span_m / 0.3048
        lat_per_ft = (bbox["max_lat"] - bbox["min_lat"]) / ns_span_ft if ns_span_ft > 0 else 0
        result = dict(bbox)
        if side == "S":
            result["min_lat"] += lat_per_ft * feet
        else:
            result["max_lat"] -= lat_per_ft * feet
        return result


def parse_aliquot_chain(text: str) -> List[str]:
    """Extract subdivision chain from aliquot description.

    "THE SOUTH 1/2 OF THE NORTHEAST 1/4 OF THE SOUTHEAST 1/4"
    → ['S', 'NE', 'SE']  (applied right-to-left: SE first, then NE, then S)
    """
    # Normalize
    t = text.upper()
    # Remove articles and prepositions for cleaner parsing
    t = re.sub(r"\bONE[- ](?:HALF|QUARTER)\b", "", t)

    chain = []
    # Find all subdivision tokens
    pattern = r"(NORTH\s*EAST|SOUTH\s*EAST|NORTH\s*WEST|SOUTH\s*WEST|NORTH|SOUTH|EAST|WEST|NE|NW|SE|SW|N|S|E|W)\s*(?:1/[24]|HALF|QUARTER)"
    for m in re.finditer(pattern, t):
        direction = m.group(1).strip()
        direction = direction.replace(" ", "")
        # Normalize to 1-2 letter codes
        mapping = {
            "NORTHEAST": "NE", "SOUTHEAST": "SE", "NORTHWEST": "NW", "SOUTHWEST": "SW",
            "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
        }
        direction = mapping.get(direction, direction)
        chain.append(direction)
    return chain


def parse_section_ref(text: str) -> Optional[Tuple[int, int, int]]:
    """Extract (township, range, section) from text."""
    m = re.search(
        r"SECTION\s+(\d+)\s*,?\s*TOWNSHIP\s+(\d+)\s+SOUTH\s*,?\s*RANGE\s+(\d+)\s+EAST",
        text, re.IGNORECASE,
    )
    if m:
        return (int(m.group(2)), int(m.group(3)), int(m.group(1)))
    return None


def compute_aliquot_polygon(description: str, twp: int, rge: int, sec: int) -> Optional[List[List[float]]]:
    """Compute polygon for an aliquot description within a section."""
    bbox = get_section_bbox(twp, rge, sec)
    if not bbox:
        return None
    chain = parse_aliquot_chain(description)
    if not chain:
        return None
    # Apply from right to left (last in chain = innermost, applied first)
    for direction in reversed(chain):
        bbox = subdivide_bbox(bbox, direction)
    return bbox_to_polygon(bbox)


# ── Metes-and-Bounds Parser ──────────────────────────────────────────────────
def parse_courses_from_text(text: str) -> List[dict]:
    """Parse metes-and-bounds courses from text.

    Returns list of: {"azimuth": float, "distance": float}
    or for curves: {"type": "curve", "radius": float, "arc": float, "direction": str, "chord_bearing": float}
    """
    courses = []
    # Split on THENCE or semicolons
    parts = re.split(r";\s*(?:THENCE|THEN)\b|;\s*$", text, flags=re.IGNORECASE)
    # Also handle "THENCE" without semicolons
    expanded = []
    for part in parts:
        sub = re.split(r"\bTHENCE\b", part, flags=re.IGNORECASE)
        expanded.extend(sub)

    for part in expanded:
        part = part.strip()
        if not part:
            continue

        # Check for curve
        curve_m = re.search(r"RADIUS\s+OF\s+([\d,]+\.?\d*)\s*(?:FEET|FT\.?)", part, re.IGNORECASE)
        arc_m = re.search(r"ARC\s+(?:LENGTH\s+|DISTANCE\s+(?:OF\s+)?)?([\d,]+\.?\d*)\s*(?:FEET|FT\.?)", part, re.IGNORECASE)
        # Compute arc from central angle + radius when arc distance is missing
        if curve_m and not arc_m:
            ca_m = re.search(
                r"CENTRAL\s+ANGLE\s+OF\s+(\d+)\s*(?:DEGREES|DEG\.?|°)\s*(\d+)\s*(?:MINUTES|MIN\.?|[\'′])\s*(?:(\d+(?:\.\d+)?)\s*(?:SECONDS|SEC\.?|[\"″])?)?",
                part, re.IGNORECASE,
            )
            if ca_m:
                ca_rad = math.radians(float(ca_m.group(1)) + float(ca_m.group(2)) / 60 + float(ca_m.group(3) or 0) / 3600)
                computed_arc = float(curve_m.group(1).replace(",", "")) * ca_rad
                class _ArcResult:
                    def group(self, n): return f"{computed_arc:.2f}"
                arc_m = _ArcResult()
        if curve_m and arc_m:
            radius = float(curve_m.group(1).replace(",", ""))
            arc = float(arc_m.group(1).replace(",", ""))

            # Extract chord bearing if explicitly stated
            chord_bearing = None
            chord_m = re.search(r"CHORD\s+BEARING\s+(?:OF\s+)?", part, re.IGNORECASE)
            if chord_m:
                chord_bearing = parse_bearing(part[chord_m.end():])
            else:
                # Some descriptions state chord bearing without "CHORD BEARING" prefix
                # Only use parse_bearing if it appears to be a chord reference
                if re.search(r"HAVING\s+A\s+CHORD", part, re.IGNORECASE):
                    chord_bearing = parse_bearing(part)

            # Extract concave direction
            concave_dir = None
            concave_m = re.search(
                r"(?:CONCAV(?:E|ITY))\s+(?:TO\s+THE\s+)?(NORTH(?:EAST|WEST)?(?:ERLY)?|SOUTH(?:EAST|WEST)?(?:ERLY)?|EAST(?:ERLY)?|WEST(?:ERLY)?)",
                part, re.IGNORECASE
            )
            if concave_m:
                concave_dir = concave_m.group(1).upper()

            # Direction defaults to "right" but will be resolved in advance_curve using concave_dir
            direction = "right"
            if re.search(r"\bLEFT\b", part, re.IGNORECASE):
                direction = "left"

            courses.append({
                "type": "curve",
                "radius": radius,
                "arc": arc,
                "direction": direction,
                "concave_dir": concave_dir,
                "chord_bearing": chord_bearing,
            })
            continue

        # Check for line with bearing and distance
        azimuth = parse_bearing(part)
        distance = parse_distance(part)

        if azimuth is not None and distance is not None:
            courses.append({"type": "line", "azimuth": azimuth, "distance": distance})
        elif azimuth is not None:
            # Bearing but no distance - could be "to the line of..." or "to POB"
            courses.append({"type": "line_to_boundary", "azimuth": azimuth})
        elif part and len(part) > 10:
            # Keep the gap as a placeholder so the inference engine can try to fill it
            courses.append({"type": "gap", "raw_text": part.strip()})
            log.warning(f"Unparseable THENCE fragment (kept as gap): {part[:80]!r}")

    return courses


def find_commencing_point(text: str) -> Optional[Tuple[str, int, int, int]]:
    """Find the starting reference corner for a metes-and-bounds description.

    Returns (corner_key, township, range, section) where corner_key is used
    to look up coordinates. Standard corners: NE/SE/NW/SW. Quarter corners
    (midpoints): N/S/E/W.
    """
    # Match COMMENCE/COMMENCING/BEGIN AT THE ... CORNER ... SECTION
    start_verb = r"(?:COMMENC(?:E|ING)|BEGIN)\s+AT\s+THE\s+"
    corner_names = (
        r"(NORTH\s*EAST|SOUTH\s*EAST|NORTH\s*WEST|SOUTH\s*WEST|NE|SE|NW|SW|"
        r"NORTH|SOUTH|EAST|WEST)"
    )
    # Optional subdivision qualifier: "OF THE SOUTH HALF", "OF THE SW 1/4", etc.
    subdivision = r"(?:\s+(?:QUARTER\s+)?CORNER(?:\s+OF\s+THE\s+(?:SOUTH|NORTH|EAST|WEST)\s+(?:HALF|1/2)|(?:\s+OF\s+THE\s+(?:SOUTH\s*WEST|SOUTH\s*EAST|NORTH\s*WEST|NORTH\s*EAST|SW|SE|NW|NE)\s+(?:QUARTER|1/4)))?)?"
    section_ref = r"\s*(?:OF\s+)?(?:SAID\s+)?SECTION\s+(\d+)"

    # Pattern 1: Full reference with TOWNSHIP/RANGE inline
    m = re.search(
        start_verb + corner_names + subdivision + section_ref +
        r"\s*[,;]?\s*(?:OF\s+)?TOWNSHIP\s+(\d+)\s+SOUTH\s*,?\s*RANGE\s+(\d+)\s+EAST",
        text, re.IGNORECASE,
    )
    if m:
        corner = _resolve_compound_corner(m.group(0))
        return corner, int(m.group(3)), int(m.group(4)), int(m.group(2))

    # Pattern 2: "SAID SECTION" — corner + section number, township/range found elsewhere
    m2 = re.search(
        start_verb + corner_names + subdivision + section_ref,
        text, re.IGNORECASE,
    )
    if m2:
        corner = _resolve_compound_corner(m2.group(0))
        sec = int(m2.group(2))
        # Try to find the specific township/range for this section number
        # Look for "SECTION(S) X ... TOWNSHIP Y SOUTH, RANGE Z EAST" patterns
        tr = _find_township_for_section(text, sec)
        if tr:
            return corner, tr[0], tr[1], sec

    return None


def _find_township_for_section(text: str, sec: int) -> Optional[Tuple[int, int]]:
    """Find the (township, range) associated with a section number in the text.

    Parses 'LYING IN' clauses like:
      'SECTION 2, TOWNSHIP 36 SOUTH, RANGE 39 EAST AND SECTION 35, TOWNSHIP 35 SOUTH, RANGE 39 EAST'
    to match the correct township/range for the given section.
    """
    # Find all "SECTION(S) X(, Y, AND Z), TOWNSHIP T SOUTH, RANGE R EAST" groups
    pattern = r"SECTIONS?\s+([\d,\s]+(?:\s+AND\s+\d+)?)\s*,?\s*TOWNSHIP\s+(\d+)\s+SOUTH\s*,?\s*RANGE\s+(\d+)\s+EAST"
    for m in re.finditer(pattern, text, re.IGNORECASE):
        sec_text = m.group(1)
        twp = int(m.group(2))
        rge = int(m.group(3))
        # Parse section numbers from "34 AND 35" or "2, 3" etc.
        sec_nums = [int(s) for s in re.findall(r"\d+", sec_text)]
        if sec in sec_nums:
            return twp, rge
    # Fallback: first township/range found
    tr = re.search(r"TOWNSHIP\s+(\d+)\s+SOUTH\s*,?\s*RANGE\s+(\d+)\s+EAST", text, re.IGNORECASE)
    if tr:
        return int(tr.group(1)), int(tr.group(2))
    return None


def _normalize_corner(raw: str) -> str:
    """Normalize corner name to NE/SE/NW/SW or N/S/E/W (for quarter corners)."""
    c = raw.upper().replace(" ", "")
    mapping = {"NORTHEAST": "NE", "SOUTHEAST": "SE", "NORTHWEST": "NW", "SOUTHWEST": "SW",
               "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W"}
    return mapping.get(c, c)


def _resolve_compound_corner(match_text: str) -> str:
    """Resolve compound corner references like 'NW CORNER OF THE SOUTH HALF' to a corner key.

    Maps to NE/SE/NW/SW (section corners) or N/S/E/W (quarter corners = midpoints).
    """
    t = match_text.upper()
    # Extract the primary corner direction
    m = re.search(r"AT\s+THE\s+(NORTH\s*EAST|SOUTH\s*EAST|NORTH\s*WEST|SOUTH\s*WEST|NE|SE|NW|SW|NORTH|SOUTH|EAST|WEST)", t)
    if not m:
        return "NW"
    primary = _normalize_corner(m.group(1))

    # Check for "QUARTER CORNER" (e.g., "NORTH QUARTER CORNER") — this IS a quarter corner
    if re.search(r"QUARTER\s+CORNER", t):
        return primary  # N/S/E/W

    # Check for subdivision: "CORNER OF THE [direction] [HALF|QUARTER]"
    sub = re.search(r"CORNER\s+OF\s+THE\s+(SOUTH|NORTH|EAST|WEST|SOUTH\s*WEST|SOUTH\s*EAST|NORTH\s*WEST|NORTH\s*EAST|SW|SE|NW|NE)\s+(HALF|1/2|QUARTER|1/4)", t)
    if sub:
        sub_dir = _normalize_corner(sub.group(1))
        sub_type = sub.group(2)
        # Compute the actual corner position within the subdivision
        # The subdivision defines a sub-rectangle; we want the primary corner of that sub-rectangle
        # For half sections:
        #   S HALF: corners are W(midpoint), E(midpoint), SW, SE
        #   N HALF: corners are NW, NE, W(midpoint), E(midpoint)
        #   E HALF: corners are N(midpoint), NE, S(midpoint), SE
        #   W HALF: corners are NW, N(midpoint), SW, S(midpoint)
        # For quarter sections (e.g., SW 1/4):
        #   NW corner of SW 1/4 = W quarter corner
        half_corner_map = {
            # (primary_corner, subdivision) -> effective corner key
            # South half
            ("NW", "S"): "W", ("NE", "S"): "E", ("SW", "S"): "SW", ("SE", "S"): "SE",
            # North half
            ("NW", "N"): "NW", ("NE", "N"): "NE", ("SW", "N"): "W", ("SE", "N"): "E",
            # East half
            ("NW", "E"): "N", ("NE", "E"): "NE", ("SW", "E"): "S", ("SE", "E"): "SE",
            # West half
            ("NW", "W"): "NW", ("NE", "W"): "N", ("SW", "W"): "SW", ("SE", "W"): "S",
            # Quarter sections
            ("NW", "SW"): "W", ("NE", "SW"): "C", ("SW", "SW"): "SW", ("SE", "SW"): "S",
            ("NW", "SE"): "C", ("NE", "SE"): "E", ("SW", "SE"): "S", ("SE", "SE"): "SE",
            ("NW", "NW"): "NW", ("NE", "NW"): "N", ("SW", "NW"): "W", ("SE", "NW"): "C",
            ("NW", "NE"): "N", ("NE", "NE"): "NE", ("SW", "NE"): "C", ("SE", "NE"): "E",
        }
        result = half_corner_map.get((primary, sub_dir))
        if result:
            return result

    return primary


def _azimuth_to_bearing_str(azimuth: float) -> str:
    """Convert azimuth (0-360) back to surveyor bearing string."""
    if azimuth <= 90:
        ns, ew, angle = "N", "E", azimuth
    elif azimuth <= 180:
        ns, ew, angle = "S", "E", 180 - azimuth
    elif azimuth <= 270:
        ns, ew, angle = "S", "W", azimuth - 180
    else:
        ns, ew, angle = "N", "W", 360 - azimuth
    deg = int(angle)
    minutes = int((angle - deg) * 60)
    sec = ((angle - deg) * 60 - minutes) * 60
    return f"{ns} {deg:02d}\u00b0{minutes:02d}'{sec:05.2f}\" {ew}"


def _build_leg(leg_num, phase, course, s_lat, s_lon, e_lat, e_lon, raw_text=""):
    """Build a structured leg record from a parsed course."""
    return {
        "leg_num": leg_num,
        "phase": phase,
        "type": course["type"],
        "raw_text": raw_text[:200],
        "bearing_raw": _azimuth_to_bearing_str(course["azimuth"]) if course.get("azimuth") is not None else None,
        "azimuth": course.get("azimuth"),
        "distance_ft": course.get("distance"),
        "radius_ft": course.get("radius"),
        "arc_ft": course.get("arc"),
        "concave_dir": course.get("concave_dir"),
        "chord_bearing": course.get("chord_bearing"),
        "start_lat": round(s_lat, 8), "start_lon": round(s_lon, 8),
        "end_lat": round(e_lat, 8), "end_lon": round(e_lon, 8),
        "flags": ["inferred:" + course["inferred"]] if course.get("inferred") else [],
        "confidence": 0.5 if course.get("inferred") else 1.0,
        "inferred": course.get("inferred"),
    }


def _ring_is_cw(coords: List[List[float]]) -> bool:
    """Check if a [lon, lat] coordinate ring has clockwise winding (Shoelace formula)."""
    area2 = 0.0
    n = len(coords)
    for i in range(n):
        j = (i + 1) % n
        area2 += coords[i][0] * coords[j][1]
        area2 -= coords[j][0] * coords[i][1]
    return area2 < 0  # Negative = CW in lon/lat space


def traverse_metes_bounds(text: str) -> Optional[dict]:
    """Full metes-and-bounds processing. Returns coordinates, legs table, and closure error."""
    ref = find_commencing_point(text)
    if not ref:
        return None

    corner, twp, rge, sec = ref
    corners = _PLSS_INDEX.get((twp, rge, sec))
    if not corners:
        return None

    if corner in ("N", "S", "E", "W"):
        midpoint_map = {
            "N": ("NW", "NE"), "S": ("SW", "SE"),
            "E": ("NE", "SE"), "W": ("NW", "SW"),
        }
        c1, c2 = midpoint_map[corner]
        start_lat = (corners[c1][0] + corners[c2][0]) / 2
        start_lon = (corners[c1][1] + corners[c2][1]) / 2
    elif corner == "C":
        start_lat, start_lon = corners["C"]
    else:
        start_lat, start_lon = corners[corner]

    pob_split = re.split(r"(?:THE\s+)?(?:POINT\s+OF\s+BEGINNING|P\.?\s*O\.?\s*B\.?)", text, flags=re.IGNORECASE)

    has_commence = bool(re.search(r"\bCOMMENC(?:E|ING)\b", text, re.IGNORECASE))
    if not has_commence:
        boundary_text = text
        commencing_text = ""
    elif len(pob_split) < 2:
        return None
    else:
        commencing_text = pob_split[0]
        boundary_text = "".join(pob_split[1:])

    # Split raw text into THENCE fragments for leg raw_text
    comm_frags = re.split(r"\bTHENCE\b", commencing_text, flags=re.IGNORECASE)
    bnd_frags = re.split(r"\bTHENCE\b", boundary_text, flags=re.IGNORECASE)
    # Check if pre-THENCE text contains a parseable course (adjusts raw text offset)
    _pre_thence_has_course = bool(
        bnd_frags and (parse_bearing(bnd_frags[0]) is not None) and (parse_distance(bnd_frags[0]) is not None)
    )

    legs = []
    leg_num = 0

    # ── Commencing courses (find POB) ──
    commencing_courses = parse_courses_from_text(commencing_text)
    trav = GeodeticTraverse()
    lat, lon = start_lat, start_lon

    for i, c in enumerate(commencing_courses):
        s_lat, s_lon = lat, lon
        if c["type"] == "line":
            lat, lon = trav.advance(lat, lon, c["azimuth"], c["distance"])
        raw = comm_frags[i + 1].strip() if i + 1 < len(comm_frags) else ""
        legs.append(_build_leg(leg_num, "commencing", c, s_lat, s_lon, lat, lon, raw))
        leg_num += 1

    pob_lat, pob_lon = lat, lon

    # ── Boundary courses ──
    boundary_courses = parse_courses_from_text(boundary_text)
    if not boundary_courses:
        return None

    # ── Gap resolution: fill in unparseable fragments ──
    gap_report = []
    if any(c["type"] == "gap" for c in boundary_courses):
        boundary_courses, gap_report = resolve_gaps(
            boundary_courses, boundary_text, trav, pob_lat, pob_lon,
        )
        for g in gap_report:
            log.info(f"Gap at index {g['gap_index']}: {g['method']} -> {g['result']}")

    points = [(pob_lat, pob_lon)]
    section_bbox = get_section_bbox(twp, rge, sec)

    for i, c in enumerate(boundary_courses):
        s_lat, s_lon = lat, lon
        # When pre-THENCE text contains a course, fragments align starting at index 0
        frag_idx = i if _pre_thence_has_course else i + 1
        raw = bnd_frags[frag_idx].strip() if frag_idx < len(bnd_frags) else ""

        if c["type"] == "line":
            lat, lon = trav.advance(lat, lon, c["azimuth"], c["distance"])
            points.append((lat, lon))
        elif c["type"] == "curve":
            arc_points = trav.advance_curve(
                lat, lon, c["radius"], c["arc"], c["direction"],
                c.get("chord_bearing"), c.get("concave_dir")
            )
            if arc_points:
                points.extend(arc_points)
                lat, lon = arc_points[-1]
        elif c["type"] == "line_to_boundary":
            azimuth = c["azimuth"]
            if i == len(boundary_courses) - 1:
                _, _, dist_m = trav.geod.inv(lon, lat, pob_lon, pob_lat)
                lat, lon = pob_lat, pob_lon
            else:
                if section_bbox and (350 < azimuth or azimuth < 10 or 170 < azimuth < 190):
                    target_lat = section_bbox["max_lat"] if azimuth < 90 or azimuth > 270 else section_bbox["min_lat"]
                    mid_lat = (section_bbox["min_lat"] + section_bbox["max_lat"]) / 2
                    for candidate in [mid_lat, (mid_lat + section_bbox["max_lat"]) / 2,
                                      (mid_lat + section_bbox["min_lat"]) / 2]:
                        if (azimuth < 90 or azimuth > 270) and candidate > lat:
                            target_lat = candidate
                            break
                        elif 90 < azimuth < 270 and candidate < lat:
                            target_lat = candidate
                            break
                    _, _, dist_m = trav.geod.inv(lon, lat, lon, target_lat)
                    dist_ft = dist_m / 0.3048
                    lat, lon = trav.advance(lat, lon, azimuth, dist_ft)
                else:
                    lat, lon = trav.advance(lat, lon, azimuth, 100)
            points.append((lat, lon))

        legs.append(_build_leg(leg_num, "boundary", c, s_lat, s_lon, lat, lon, raw))
        leg_num += 1

    # Closure error before adjustment
    _, _, closure_m = trav.geod.inv(lon, lat, pob_lon, pob_lat)
    closure_ft = closure_m / 0.3048

    # Apply compass rule
    points = trav.apply_compass_rule(points, pob_lat, pob_lon)

    coords = [[p[1], p[0]] for p in points]
    if coords[0] != coords[-1]:
        coords.append(coords[0])

    # Ensure CCW winding for exterior ring per RFC 7946
    if _ring_is_cw(coords):
        coords.reverse()

    return {
        "coordinates": coords,
        "pob": [pob_lon, pob_lat],
        "section_ref": f"T{twp}S R{rge}E Sec {sec}",
        "legs": legs,
        "closure_ft": round(closure_ft, 2),
        "num_courses": len(boundary_courses),
        "gaps_resolved": len(gap_report),
        "gap_report": gap_report,
    }


# ── PDF Text Extraction with OCR Fallback ────────────────────────────────────
_gemini_client = None

def _get_gemini_client():
    """Lazy-init singleton Gemini client."""
    global _gemini_client
    if _gemini_client is None:
        try:
            from google import genai
            api_key = os.environ.get("GEMINI_API_KEY")
            if not api_key:
                return None
            _gemini_client = genai.Client(api_key=api_key)
        except ImportError:
            return None
    return _gemini_client


def _gemini_ocr_page(page_image_bytes: bytes) -> str:
    """Use Gemini Vision API to OCR a scanned PDF page."""
    try:
        from google.genai import types
        client = _get_gemini_client()
        if not client:
            return ""
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=[
                "Extract ALL text from this scanned document page exactly as written. "
                "Preserve all numbers, degree symbols (°), minutes ('), seconds (\"), "
                "bearing directions (N/S/E/W), distances, and legal description formatting. "
                "Do not summarize or interpret — just transcribe the text.",
                types.Part.from_bytes(data=page_image_bytes, mime_type="image/png"),
            ],
        )
        return response.text if response.text else ""
    except Exception as e:
        print(f"  Gemini OCR failed: {e}")
        return ""


def extract_text_from_pdf(pdf_path: Path, verbose: bool = False) -> str:
    """Extract text from PDF. Falls back to Gemini Vision OCR for scanned/image pages."""
    doc = fitz.open(pdf_path)
    text = ""
    ocr_pages = 0
    for i, page in enumerate(doc):
        page_text = page.get_text()
        if len(page_text.strip()) > 20:
            text += page_text + "\n"
        else:
            # Page is likely a scanned image — try Gemini OCR
            if verbose:
                print(f"  Page {i+1}: no text, attempting Gemini OCR...")
            pix = page.get_pixmap(dpi=300)
            ocr_text = _gemini_ocr_page(pix.tobytes("png"))
            if ocr_text:
                text += ocr_text + "\n"
                ocr_pages += 1
                if verbose:
                    print(f"  Page {i+1}: OCR recovered {len(ocr_text)} chars")
            elif verbose:
                print(f"  Page {i+1}: OCR returned nothing")
    if verbose and ocr_pages:
        print(f"  Gemini OCR used on {ocr_pages} page(s)")
    return text


def split_into_parcels(text: str) -> List[dict]:
    """Split ordinance text into individual parcel descriptions."""
    parcels = []

    # Find parcel headers in various formats:
    #   ("PARCEL 4")  or  {"PARCEL 6"}  — paren/brace style
    #   Parcel B:     or  PARCEL 1:     — colon style
    #   LEGAL DESCRIPTION: PROPOSED PHASE 1 A — phase-style (CDD ordinances)
    parcel_pattern = r'(?:[(\{\"\'][\s\"\']*PARCEL\s+(\w+)["\s]*(?:PER\s+DEED[^)}\n]*)?[)\}]+|(?:^|\n)\s*PARCEL\s+(\w+)\s*:)'
    phase_pattern = r'LEGAL\s+DESCRIPTION\s*:\s*(?:PROPOSED\s+)?PHASE\s+([\w\s]+?)(?=\s*\n|\s+A\s+PARCEL)'
    parcel_starts_raw = list(re.finditer(parcel_pattern, text, re.IGNORECASE))
    phase_starts_raw = list(re.finditer(phase_pattern, text, re.IGNORECASE))
    # Normalize: extract (match, parcel_id) tuples
    parcel_starts = []
    for m in parcel_starts_raw:
        parcel_id = m.group(1) or m.group(2)
        parcel_starts.append((m, f"Parcel {parcel_id}"))
    for m in phase_starts_raw:
        phase_id = m.group(1).strip()
        parcel_starts.append((m, f"Phase {phase_id}"))
    # Sort by position in text and deduplicate overlapping ranges
    parcel_starts.sort(key=lambda x: x[0].start())
    # Remove duplicates (same phase/parcel appearing in ordinance text + exhibit)
    seen_labels = set()
    unique_starts = []
    for match, label in parcel_starts:
        if label not in seen_labels:
            seen_labels.add(label)
            unique_starts.append((match, label))
    parcel_starts = unique_starts

    # Also find standalone metes-and-bounds blocks (various intro patterns).
    # A metes-and-bounds description has at least two POB references:
    #   1. "...TO THE POINT OF BEGINNING; THENCE..." (commencing ends, boundary starts)
    #   2. "...TO THE POINT OF BEGINNING." (boundary closes back)
    # We capture from the intro through the CLOSING (last relevant) POB.
    intro_pattern = r"(?:A\s+PORTION\s+OF\s+LAND|BEING\s+ALL\s+OF\b|BEING\s+A\s+PART\s+OF\b)"
    pob_pattern = r"(?:POINT\s+OF\s+BEGINNING|P\.?\s*O\.?\s*B\.?)"
    standalone_blocks = []
    for intro_m in re.finditer(intro_pattern, text, re.IGNORECASE):
        start = intro_m.start()
        # Find all POB occurrences after this intro
        pob_hits = list(re.finditer(pob_pattern, text[start:], re.IGNORECASE))
        if not pob_hits:
            continue
        # Use the second POB (closing) if available, otherwise the first
        closing_pob = pob_hits[1] if len(pob_hits) >= 2 else pob_hits[0]
        end = start + closing_pob.end()
        # Extend past trailing "CONTAINING ... ACRES MORE OR LESS." if present
        after = text[end:end + 300]
        acreage_m = re.match(
            r"[\s.;]*CONTAINING\s+[\d,.]+\s+(?:SQUARE\s+FEET|ACRES).*?(?:MORE\s+OR\s+LESS)?[.\n]",
            after, re.IGNORECASE,
        )
        if acreage_m:
            end += acreage_m.end()
        standalone_blocks.append({"start": start, "end": end, "text": text[start:end].strip()})

    # Build a list of all text blocks with their positions
    blocks = []
    for i, (match, label) in enumerate(parcel_starts):
        start = match.start()
        end = parcel_starts[i + 1][0].start() if i + 1 < len(parcel_starts) else len(text)
        blocks.append({
            "label": label,
            "start": start,
            "end": end,
            "text": text[start:end].strip(),
        })

    # Check if any standalone metes-and-bounds blocks exist outside numbered parcels
    standalone_idx = 0
    for sb in standalone_blocks:
        inside_parcel = any(b["start"] <= sb["start"] < b["end"] for b in blocks)
        if not inside_parcel:
            standalone_idx += 1
            label = f"Boundary {standalone_idx}" if len(standalone_blocks) > 1 else "Boundary"
            if blocks and "PORTION OF LAND" in sb["text"].upper():
                label = "Road Parcel (FDOT)"
            blocks.append({
                "label": label,
                "start": sb["start"],
                "end": sb["end"],
                "text": sb["text"],
            })

    # Now classify and parse each block
    for block in blocks:
        parcel_text = block["text"]
        label = block["label"]

        # Check if this block contains BOTH an aliquot description AND a standalone
        # metes-and-bounds (e.g., Parcel 10 followed by FDOT road parcel)
        portion_split = re.split(r"(?=A\s+PORTION\s+OF\s+LAND)", parcel_text, flags=re.IGNORECASE)
        if len(portion_split) > 1 and re.search(r"\b[12]/[24]\b", portion_split[0]):
            # First part is aliquot, rest is standalone metes-and-bounds
            _add_aliquot_parcel(parcels, label, portion_split[0])
            for extra in portion_split[1:]:
                if re.search(r"\bCOMMENCE\b", extra, re.IGNORECASE):
                    _add_metes_bounds_parcel(parcels, "Road Parcel (FDOT)", extra)
            continue

        # Classify: has COMMENCE/BEGIN AT → metes-and-bounds, has fractions → aliquot
        has_commence = bool(re.search(r"\bCOMMENC(?:E|ING)\b", parcel_text, re.IGNORECASE))
        has_begin_at = bool(re.search(r"\bBEGIN\s+AT\s+THE\b", parcel_text, re.IGNORECASE))

        if has_commence or has_begin_at:
            _add_metes_bounds_parcel(parcels, label, parcel_text)
        else:
            _add_aliquot_parcel(parcels, label, parcel_text)

    return parcels


def _add_metes_bounds_parcel(parcels: list, label: str, parcel_text: str):
    """Add a metes-and-bounds parcel, handling LESS AND EXCEPT sub-parcels."""
    less_except_split = re.split(
        r"LESS\s+AND\s+EXCEPT\s+THE\s+FOLLOWING", parcel_text, flags=re.IGNORECASE
    )
    parcels.append({
        "label": label,
        "type": "metes_and_bounds",
        "text": less_except_split[0],
    })
    for j, except_text in enumerate(less_except_split[1:]):
        if re.search(r"\bCOMMENCE\b", except_text, re.IGNORECASE):
            parcels.append({
                "label": f"{label} (Less & Except {j+1})",
                "type": "metes_and_bounds",
                "text": except_text,
                "exclusion": True,
            })


def _add_aliquot_parcel(parcels: list, label: str, parcel_text: str):
    """Add an aliquot parcel, handling compound descriptions and exclusions."""
    if not re.search(r"\b[12]/[24]\b", parcel_text):
        return

    sec_ref = parse_section_ref(parcel_text)

    # Handle "LESS AND EXCEPT THE WEST 52.50 FEET"
    less_feet = None
    less_side = None
    less_m = re.search(
        r"LESS\s+AND\s+EXCEPT\s+THE\s+(WEST|EAST|NORTH|SOUTH)\s+([\d.]+)\s*FEET",
        parcel_text, re.IGNORECASE,
    )
    if less_m:
        less_side = less_m.group(1).upper()[0]
        less_feet = float(less_m.group(2))

    # Find the section reference location to split description from boilerplate
    section_suffix_match = re.search(
        r"OF\s+SECTION\s+\d+.*?(?:FLORIDA)", parcel_text, re.IGNORECASE | re.DOTALL
    )

    # Get the description text before the section reference
    pre_section = parcel_text[:section_suffix_match.start()] if section_suffix_match else parcel_text
    # Clean up: remove the parcel header
    pre_section = re.sub(r'^[(\{\"\'].*?[)\}]+\s*', '', pre_section, flags=re.IGNORECASE)

    # Split compound descriptions on ", AND " or " AND THE " but not "LESS AND EXCEPT"
    # First remove "LESS AND EXCEPT" clauses
    clean = re.sub(r"LESS\s+AND\s+EXCEPT.*$", "", pre_section, flags=re.IGNORECASE).strip()
    sub_descs = re.split(r",?\s+AND\s+(?:THE\s+)?", clean, flags=re.IGNORECASE)
    sub_descs = [s.strip() for s in sub_descs if s.strip() and re.search(r"\b[12]/[24]\b", s)]

    parcels.append({
        "label": label,
        "type": "aliquot",
        "sub_descriptions": sub_descs if sub_descs else [parcel_text],
        "section_ref": sec_ref,
        "text": parcel_text,
        "less_feet": less_feet,
        "less_side": less_side,
    })


# ── Main Processing Pipeline ─────────────────────────────────────────────────
def extract_document_name(text: str, pdf_path: Path) -> str:
    """Try to extract a meaningful name from the PDF text."""
    # Try CDD name
    m = re.search(r"(?:THE\s+)([\w\s]+?)\s+COMMUNITY\s+DEVELOPMENT\s+DISTRICT", text, re.IGNORECASE)
    if m:
        raw = re.sub(r"\s+", " ", m.group(1).strip())
        raw = re.sub(r"^(?:ESTABLISHING|ENTITLED|CREATING)\s+(?:THE\s+)?", "", raw, flags=re.IGNORECASE).strip()
        return raw + " CDD"
    # Fallback to filename
    return pdf_path.stem.replace("_", " ").replace("-", " ").title()


def process_ordinance(pdf_path: Path, verbose: bool = False) -> dict:
    """Process a PDF with legal descriptions and return GeoJSON FeatureCollection."""
    text = extract_text_from_pdf(pdf_path, verbose=verbose)

    doc_name = extract_document_name(text, pdf_path)

    if verbose:
        print(f"Document: {doc_name}")
        print(f"Text length: {len(text)} chars")

    parcels = split_into_parcels(text)
    if verbose:
        print(f"Parcels found: {len(parcels)}")
        for p in parcels:
            print(f"  {p['label']} ({p['type']})")

    features = []
    for parcel in parcels:
        try:
            if parcel["type"] == "aliquot":
                sec_ref = parcel.get("section_ref")
                if not sec_ref:
                    if verbose:
                        print(f"  SKIP {parcel['label']}: no section reference found")
                    continue

                twp, rge, sec = sec_ref
                combined_coords = []

                for sub_desc in parcel.get("sub_descriptions", [parcel["text"]]):
                    chain = parse_aliquot_chain(sub_desc)
                    if not chain:
                        continue
                    bbox = get_section_bbox(twp, rge, sec)
                    if not bbox:
                        if verbose:
                            print(f"  SKIP {parcel['label']}: section T{twp}S R{rge}E Sec {sec} not in PLSS data")
                        continue

                    for direction in reversed(chain):
                        bbox = subdivide_bbox(bbox, direction)

                    # Handle "SOUTH 13 FEET OF" type descriptions
                    feet_m = re.search(r"(SOUTH|NORTH)\s+(\d+(?:\.\d+)?)\s*FEET\s+OF", sub_desc, re.IGNORECASE)
                    if feet_m:
                        side = feet_m.group(1)[0].upper()
                        feet = float(feet_m.group(2))
                        bbox = trim_bbox_feet(bbox, side, feet)

                    combined_coords.append(bbox)

                if not combined_coords:
                    continue

                # Merge bboxes: take the overall bounding box of all sub-descriptions
                merged = {
                    "min_lat": min(b["min_lat"] for b in combined_coords),
                    "max_lat": max(b["max_lat"] for b in combined_coords),
                    "min_lon": min(b["min_lon"] for b in combined_coords),
                    "max_lon": max(b["max_lon"] for b in combined_coords),
                }

                # Apply "less and except" side trimming
                if parcel.get("less_feet") and parcel.get("less_side"):
                    merged = trim_bbox_from_side(merged, parcel["less_side"], parcel["less_feet"])

                coords = bbox_to_polygon(merged)
                features.append({
                    "type": "Feature",
                    "properties": {
                        "name": doc_name,
                        "parcel": parcel["label"],
                        "desc_type": "aliquot",
                        "section": f"T{twp}S R{rge}E Sec {sec}",
                    },
                    "geometry": {"type": "Polygon", "coordinates": [coords]},
                })
                if verbose:
                    print(f"  OK {parcel['label']}: aliquot polygon generated")

            elif parcel["type"] == "metes_and_bounds":
                result = traverse_metes_bounds(parcel["text"])
                if result and len(result["coordinates"]) >= 4:
                    props = {
                        "name": doc_name,
                        "parcel": parcel["label"],
                        "desc_type": "metes_and_bounds",
                        "section": result["section_ref"],
                        "closure_ft": result.get("closure_ft"),
                        "num_courses": result.get("num_courses"),
                    }
                    if parcel.get("exclusion"):
                        props["exclusion"] = True
                    feat = {
                        "type": "Feature",
                        "properties": props,
                        "geometry": {"type": "Polygon", "coordinates": [result["coordinates"]]},
                    }
                    if result.get("legs"):
                        feat["_legs"] = result["legs"]
                        feat["_raw_text"] = parcel["text"]
                    features.append(feat)
                    if verbose:
                        print(f"  OK {parcel['label']}: {len(result['coordinates'])} vertices, "
                              f"{result.get('num_courses', '?')} courses, "
                              f"closure: {result.get('closure_ft', '?')} ft")
                else:
                    if verbose:
                        reason = "no result" if not result else f"only {len(result['coordinates'])} vertices"
                        print(f"  FAIL {parcel['label']}: {reason}")

        except Exception as e:
            if verbose:
                print(f"  ERROR {parcel['label']}: {e}")
                traceback.print_exc()

    return {"type": "FeatureCollection", "features": features}


# ── Output Helpers ────────────────────────────────────────────────────────────
def create_shapefile(geojson: dict, output_prefix: Path):
    w = shapefile.Writer(str(output_prefix))
    w.field("NAME", "C", size=100)
    w.field("PARCEL", "C", size=100)
    w.field("DESC_TYPE", "C", size=30)
    w.field("SECTION", "C", size=30)
    w.field("CLOSURE_FT", "N", decimal=2)
    w.field("NUM_COURSE", "N")

    for feat in geojson["features"]:
        coords = feat["geometry"]["coordinates"][0]
        w.poly([coords])
        p = feat["properties"]
        w.record(
            p.get("name", ""), p.get("parcel", ""), p.get("desc_type", ""),
            p.get("section", ""), p.get("closure_ft"), p.get("num_courses"),
        )

    w.close()
    prj = 'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]'
    output_prefix.with_suffix(".prj").write_text(prj)


def create_viewer_html(geojson: dict, output_path: Path, doc_name: str):
    """Create a standalone Leaflet HTML viewer for the results."""
    # Compute center from all coordinates
    all_lats, all_lons = [], []
    for feat in geojson["features"]:
        for coord in feat["geometry"]["coordinates"][0]:
            all_lons.append(coord[0])
            all_lats.append(coord[1])

    if not all_lats:
        return

    center_lat = (min(all_lats) + max(all_lats)) / 2
    center_lon = (min(all_lons) + max(all_lons)) / 2

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>{doc_name} - Generated Boundaries</title>
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
    <style>
        body {{ margin: 0; font-family: system-ui; }}
        #map {{ height: 100vh; }}
        .info {{ position: absolute; top: 10px; right: 10px; z-index: 1000; background: white;
                 padding: 14px 18px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.25);
                 max-width: 380px; font-size: 0.9rem; }}
        .info h3 {{ margin: 0 0 8px; }}
        .legend-item {{ margin: 4px 0; display: flex; align-items: center; gap: 8px; }}
        .legend-swatch {{ width: 16px; height: 16px; border-radius: 3px; border: 1px solid #aaa; }}
    </style>
</head>
<body>
<div id="map"></div>
<div class="info">
    <h3>{doc_name}</h3>
    <div id="legend"></div>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const geojson = {json.dumps(geojson)};
const map = L.map('map').setView([{center_lat}, {center_lon}], 14);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
    attribution: '&copy; OpenStreetMap'
}}).addTo(map);

const colors = ['#e74c3c','#3498db','#2ecc71','#f39c12','#9b59b6','#e67e22','#1abc9c','#e84393','#00b894','#6c5ce7'];
const legend = document.getElementById('legend');

geojson.features.forEach((feat, i) => {{
    const color = feat.properties.exclusion ? '#999' : colors[i % colors.length];
    const dashArray = feat.properties.exclusion ? '8 4' : null;
    L.geoJSON(feat, {{
        style: {{ color: color, weight: 3, fillOpacity: feat.properties.exclusion ? 0.1 : 0.2,
                  fillColor: color, dashArray: dashArray }},
        onEachFeature: (f, l) => {{
            l.bindPopup('<b>' + f.properties.parcel + '</b><br>Type: ' + f.properties.desc_type +
                       '<br>Section: ' + f.properties.section +
                       (f.properties.exclusion ? '<br><em>(Exclusion area)</em>' : ''));
        }}
    }}).addTo(map);

    const div = document.createElement('div');
    div.className = 'legend-item';
    div.innerHTML = '<div class="legend-swatch" style="background:' + color + (feat.properties.exclusion ? ';opacity:0.3' : '') +
                    '"></div><span>' + feat.properties.parcel + ' (' + feat.properties.desc_type + ')</span>';
    legend.appendChild(div);
}});

if (geojson.features.length > 0) {{
    map.fitBounds(L.geoJSON(geojson).getBounds().pad(0.2));
}}
</script>
</body>
</html>"""
    output_path.write_text(html)


# ── CLI Mode ──────────────────────────────────────────────────────────────────
def cli_process(pdf_path_str: str):
    pdf_path = Path(pdf_path_str)
    if not pdf_path.exists():
        print(f"ERROR: File not found: {pdf_path}")
        sys.exit(1)

    print(f"Processing: {pdf_path.name}")
    print("=" * 60)

    geojson = process_ordinance(pdf_path, verbose=True)

    if not geojson["features"]:
        print("\nNo features generated. The legal descriptions may not be parseable.")
        sys.exit(1)

    # Create output directory
    slug = re.sub(r"[^a-z0-9]+", "_", pdf_path.stem.lower()).strip("_")
    out_dir = WORK_DIR / slug
    out_dir.mkdir(exist_ok=True)

    # Extract legs and raw text from features (before stripping for clean GeoJSON)
    all_legs = {}
    raw_texts = {}
    for feat in geojson["features"]:
        parcel_name = feat["properties"]["parcel"]
        if "_legs" in feat:
            all_legs[parcel_name] = feat.pop("_legs")
        if "_raw_text" in feat:
            raw_texts[parcel_name] = feat.pop("_raw_text")

    # Save legs table
    if all_legs:
        legs_path = out_dir / "legs.json"
        legs_path.write_text(json.dumps(all_legs, indent=2))

    # Run LLM validation on legs (requires GEMINI_API_KEY)
    if all_legs:
        try:
            from validate_legs import validate_legs
            validated_legs = {}
            for parcel_name, legs in all_legs.items():
                raw_text = raw_texts.get(parcel_name, "")
                print(f"  Validating {parcel_name} ({len(legs)} legs)...")
                validated = validate_legs(legs, raw_text, verbose=False)
                validated_legs[parcel_name] = validated
                flagged = [l for l in validated if l.get("flags")]
                if flagged:
                    print(f"    {len(flagged)} leg(s) flagged")
                    for l in flagged:
                        flags_str = ', '.join(l['flags'][:2])
                        # Sanitize for Windows console encoding
                        flags_str = flags_str.encode('ascii', errors='replace').decode('ascii')
                        print(f"      Leg {l['leg_num']}: conf={l['confidence']:.2f} -- {flags_str}")
                else:
                    print(f"    All {len(validated)} legs OK")
            validated_path = out_dir / "legs_validated.json"
            validated_path.write_text(json.dumps(validated_legs, indent=2))
            print(f"  legs_validated.json - validation results saved")
        except Exception as e:
            print(f"  Validation skipped: {e}")

    # Save clean GeoJSON (without _legs)
    geojson_path = out_dir / "boundary.geojson"
    geojson_path.write_text(json.dumps(geojson, indent=2))

    doc_name = geojson["features"][0]["properties"].get("name", "Boundary")

    create_shapefile(geojson, out_dir / "boundary")
    with zipfile.ZipFile(out_dir / "shapefile.zip", "w") as z:
        for ext in [".shp", ".shx", ".dbf", ".prj"]:
            f = out_dir / f"boundary{ext}"
            if f.exists():
                z.write(f, f"boundary{ext}")

    create_viewer_html(geojson, out_dir / "view_map.html", doc_name)

    print(f"\n{'=' * 60}")
    print(f"SUCCESS: {len(geojson['features'])} features generated")
    print(f"Output directory: {out_dir}")
    print(f"  boundary.geojson  - Import into QGIS / Google Earth")
    print(f"  shapefile.zip     - Import into ArcGIS / etc.")
    print(f"  view_map.html     - Open in browser to preview")
    if all_legs:
        total_legs = sum(len(v) for v in all_legs.values())
        print(f"  legs.json         - {total_legs} parsed legs for inspection")
    return geojson


# ── Web Server (FastAPI) ──────────────────────────────────────────────────────
def run_web_server():
    from fastapi import FastAPI, File, UploadFile, BackgroundTasks
    from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
    import uvicorn

    app = FastAPI()
    JOBS: Dict[str, dict] = {}

    def process_job(job_id, pdf_path):
        JOBS[job_id]["status"] = "processing"
        try:
            geojson = process_ordinance(pdf_path, verbose=False)
            job_dir = WORK_DIR / job_id
            job_dir.mkdir(exist_ok=True)

            # Extract legs/raw text, run validation, then clean GeoJSON
            all_legs = {}
            raw_texts = {}
            for feat in geojson["features"]:
                pn = feat["properties"]["parcel"]
                if "_legs" in feat:
                    all_legs[pn] = feat.pop("_legs")
                if "_raw_text" in feat:
                    raw_texts[pn] = feat.pop("_raw_text")

            if all_legs:
                (job_dir / "legs.json").write_text(json.dumps(all_legs, indent=2))
                try:
                    from validate_legs import validate_legs
                    validated_legs = {}
                    for pn, legs in all_legs.items():
                        validated_legs[pn] = validate_legs(legs, raw_texts.get(pn, ""))
                    (job_dir / "legs_validated.json").write_text(json.dumps(validated_legs, indent=2))
                except Exception:
                    pass  # Validation is best-effort in web mode

            (job_dir / "boundary.geojson").write_text(json.dumps(geojson))

            doc_name = geojson["features"][0]["properties"].get("name", "Boundary") if geojson["features"] else "Boundary"
            create_shapefile(geojson, job_dir / "boundary")
            with zipfile.ZipFile(job_dir / "shapefile.zip", "w") as z:
                for ext in [".shp", ".shx", ".dbf", ".prj"]:
                    f = job_dir / f"boundary{ext}"
                    if f.exists():
                        z.write(f, f"boundary{ext}")

            JOBS[job_id]["status"] = "completed"
            JOBS[job_id]["geojson"] = geojson
        except Exception as e:
            JOBS[job_id]["status"] = "failed"
            JOBS[job_id]["error"] = str(e)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        if (HERE / "index.html").exists():
            return (HERE / "index.html").read_text()
        return "<h1>PDF-to-GIS Boundary Converter</h1>"

    MAX_UPLOAD_MB = 50

    @app.post("/api/upload")
    async def upload_pdf(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
        contents = await file.read()
        if len(contents) > MAX_UPLOAD_MB * 1024 * 1024:
            return JSONResponse({"error": f"File too large (max {MAX_UPLOAD_MB}MB)"}, status_code=413)
        job_id = str(uuid.uuid4())[:8]
        pdf_path = WORK_DIR / f"{job_id}_upload.pdf"
        with open(pdf_path, "wb") as f:
            f.write(contents)
        JOBS[job_id] = {"status": "uploaded", "filename": file.filename}
        background_tasks.add_task(process_job, job_id, pdf_path)
        return {"job_id": job_id}

    @app.get("/api/status/{job_id}")
    async def get_status(job_id: str):
        return JOBS.get(job_id, {"status": "not_found"})

    @app.get("/api/download/{job_id}/{file_type}")
    async def download(job_id: str, file_type: str):
        job_dir = WORK_DIR / job_id
        if file_type == "geojson":
            return FileResponse(job_dir / "boundary.geojson", filename=f"{job_id}.geojson")
        if file_type == "shp":
            return FileResponse(job_dir / "shapefile.zip", filename=f"{job_id}_shp.zip")
        return {"error": "Invalid file type"}

    uvicorn.run(app, host="0.0.0.0", port=8005)


# ── Batch Mode ───────────────────────────────────────────────────────────────
def batch_process():
    """Process all PDFs in ordinances/inbox/, output results, move PDFs to processed/."""
    pdfs = sorted(INBOX.glob("*.pdf")) + sorted(INBOX.glob("*.PDF"))
    if not pdfs:
        print("No PDFs found in ordinances/inbox/")
        return

    print(f"Found {len(pdfs)} PDF(s) in inbox")
    print("=" * 60)

    for pdf_path in pdfs:
        print(f"\nProcessing: {pdf_path.name}")
        print("-" * 40)
        try:
            geojson = process_ordinance(pdf_path, verbose=True)

            if not geojson["features"]:
                print(f"  No features generated for {pdf_path.name}")
                continue

            slug = re.sub(r"[^a-z0-9]+", "_", pdf_path.stem.lower()).strip("_")
            out_dir = WORK_DIR / slug
            out_dir.mkdir(exist_ok=True)

            # Extract legs and raw text before cleaning GeoJSON
            all_legs = {}
            raw_texts = {}
            for feat in geojson["features"]:
                pn = feat["properties"]["parcel"]
                if "_legs" in feat:
                    all_legs[pn] = feat.pop("_legs")
                if "_raw_text" in feat:
                    raw_texts[pn] = feat.pop("_raw_text")

            (out_dir / "boundary.geojson").write_text(json.dumps(geojson, indent=2))

            if all_legs:
                (out_dir / "legs.json").write_text(json.dumps(all_legs, indent=2))
                # Run validation
                try:
                    from validate_legs import validate_legs
                    validated_legs = {}
                    for pn, legs in all_legs.items():
                        validated_legs[pn] = validate_legs(legs, raw_texts.get(pn, ""))
                    (out_dir / "legs_validated.json").write_text(json.dumps(validated_legs, indent=2))
                except Exception as e:
                    print(f"    Validation skipped: {e}")

            name = geojson["features"][0]["properties"].get("name", pdf_path.stem)
            create_shapefile(geojson, out_dir / "boundary")
            with zipfile.ZipFile(out_dir / "shapefile.zip", "w") as z:
                for ext in [".shp", ".shx", ".dbf", ".prj"]:
                    f = out_dir / f"boundary{ext}"
                    if f.exists():
                        z.write(f, f"boundary{ext}")

            create_viewer_html(geojson, out_dir / "view_map.html", name)

            # Move PDF to processed
            dest = PROCESSED / pdf_path.name
            shutil.move(str(pdf_path), str(dest))

            print(f"  SUCCESS: {len(geojson['features'])} features → {out_dir}")
            print(f"  PDF moved to ordinances/processed/")

        except Exception as e:
            print(f"  ERROR processing {pdf_path.name}: {e}")
            traceback.print_exc()

    print(f"\n{'=' * 60}")
    print("Batch complete.")


# ── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--batch":
        batch_process()
    elif len(sys.argv) > 1:
        cli_process(sys.argv[1])
    else:
        run_web_server()
