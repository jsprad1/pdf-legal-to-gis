"""
LLM-based validation of parsed survey traverse legs.

Uses Gemini 2.0 Flash to cross-check extracted leg data against
the raw legal description text, flagging gaps, missing legs,
bearing reversals, parsing errors, and closure issues.
"""

import json
import math
import os
import sys
from dotenv import load_dotenv
from google import genai
from google.genai import types


def _format_legs_table(legs: list[dict]) -> str:
    """Format legs into a readable text table for the LLM prompt."""
    lines = []
    header = (
        f"{'#':>3}  {'Phase':<12} {'Type':<16} {'Bearing':<18} "
        f"{'Azimuth':>8} {'Dist(ft)':>10} {'Radius':>10} "
        f"{'Arc':>10} {'Concave':>8} {'Start Lat/Lon':<26} {'End Lat/Lon':<26}"
    )
    lines.append(header)
    lines.append("-" * len(header))

    def _fmt(val, width, align=">"):
        """Format a value that might be None into a fixed-width string."""
        s = "" if val is None else str(val)
        if align == ">":
            return s.rjust(width)
        elif align == "<":
            return s.ljust(width)
        return s.ljust(width, ".")

    for leg in legs:
        start = f"{leg.get('start_lat', ''):.6f}, {leg.get('start_lon', ''):.6f}" if leg.get('start_lat') is not None else "N/A"
        end = f"{leg.get('end_lat', ''):.6f}, {leg.get('end_lon', ''):.6f}" if leg.get('end_lat') is not None else "N/A"
        lines.append(
            f"{_fmt(leg.get('leg_num'), 3)}  "
            f"{_fmt(leg.get('phase', ''), 12, '.')}  "
            f"{_fmt(leg.get('type', ''), 16, '.')}  "
            f"{_fmt(leg.get('bearing_raw') or '', 18, '.')}  "
            f"{_fmt(leg.get('azimuth'), 8)}  "
            f"{_fmt(leg.get('distance_ft'), 10)}  "
            f"{_fmt(leg.get('radius_ft'), 10)}  "
            f"{_fmt(leg.get('arc_ft'), 10)}  "
            f"{_fmt(leg.get('concave_dir') or '', 8)}  "
            f"{start:<26}  "
            f"{end:<26}"
        )
    return "\n".join(lines)


def _compute_closure_ft(legs: list[dict]) -> float | None:
    """Compute closure error between last leg endpoint and first leg start."""
    if not legs:
        return None
    first = legs[0]
    last = legs[-1]
    if any(v is None for v in [
        first.get("start_lat"), first.get("start_lon"),
        last.get("end_lat"), last.get("end_lon"),
    ]):
        return None

    # Approximate distance in feet using Haversine
    lat1 = math.radians(first["start_lat"])
    lon1 = math.radians(first["start_lon"])
    lat2 = math.radians(last["end_lat"])
    lon2 = math.radians(last["end_lon"])

    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))
    earth_radius_ft = 20_902_231  # approximate mean radius in feet
    return earth_radius_ft * c


def _build_prompt(legs: list[dict], raw_text: str) -> str:
    """Build the validation prompt for the LLM."""
    legs_table = _format_legs_table(legs)
    closure = _compute_closure_ft(legs)
    closure_str = f"{closure:.2f} ft" if closure is not None else "unable to compute"

    prompt = f"""You are a licensed surveyor reviewing parsed traverse data extracted from a legal description.

TASK: Validate the extracted legs against the raw legal description text. Check for errors and assign confidence scores.

RAW LEGAL DESCRIPTION TEXT:
---
{raw_text if raw_text else "(No raw text provided — validate internal consistency only)"}
---

EXTRACTED LEGS TABLE:
{legs_table}

COMPUTED CLOSURE ERROR: {closure_str}

CHECKS TO PERFORM:
1. **Gaps**: Does leg N's end point match leg N+1's start point? Flag any coordinate mismatches > 0.5 ft.
2. **Missing legs**: If the raw text mentions a specific number of courses (e.g., "the following 12 courses"), does our count match?
3. **Bearing reversals**: Does any leg appear to go the opposite direction from what the raw text states?
4. **Number parsing errors**: Compare extracted bearing, distance, radius, and arc values against the raw text fragments. Flag any mismatches.
5. **Closure error**: Is the closure error reasonable? For a typical CDD boundary, closure under 5 ft is acceptable, 5-50 ft is marginal, >50 ft indicates a likely error.

RESPONSE FORMAT — respond with ONLY valid JSON, no markdown fences:
{{
  "summary": "Brief overall assessment",
  "closure_assessment": "good|marginal|poor",
  "total_issues": <int>,
  "legs": [
    {{
      "leg_num": <int>,
      "confidence": <float 0.0-1.0>,
      "flags": ["description of issue 1", "description of issue 2"]
    }}
  ]
}}

RULES:
- Include ALL legs in the "legs" array, even those with no issues (flags=[], confidence close to 1.0).
- confidence=1.0 means perfectly verified, 0.0 means certainly wrong.
- If no raw text is provided, focus on internal consistency checks (gaps, closure).
- Be precise: quote the specific values that mismatch.
"""
    return prompt


def validate_legs(legs: list[dict], raw_text: str, verbose: bool = False) -> list[dict]:
    """
    Validate parsed survey traverse legs using Gemini LLM.

    Args:
        legs: List of leg dicts with fields like leg_num, phase, type,
              bearing_raw, azimuth, distance_ft, etc.
        raw_text: The full raw text of the legal description.
        verbose: If True, print progress and LLM response.

    Returns:
        The same legs list, with "flags" (list[str]) and "confidence" (float)
        keys added to each leg dict.
    """
    load_dotenv()
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY not found in environment. "
            "Set it in .env or export it."
        )

    client = genai.Client(api_key=api_key)

    prompt = _build_prompt(legs, raw_text)

    if verbose:
        print(f"[validate_legs] Sending {len(legs)} legs to Gemini for validation...")
        print(f"[validate_legs] Raw text length: {len(raw_text)} chars")

    # Request JSON response
    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.1,
        ),
    )

    response_text = response.text.strip()

    if verbose:
        print(f"[validate_legs] Raw LLM response:\n{response_text}\n")

    # Parse the JSON response
    try:
        result = json.loads(response_text)
    except json.JSONDecodeError as e:
        # Try to extract JSON from markdown fences if present
        import re
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", response_text, re.DOTALL)
        if match:
            result = json.loads(match.group(1))
        else:
            raise ValueError(
                f"Could not parse LLM response as JSON: {e}\n"
                f"Response was: {response_text[:500]}"
            )

    if verbose:
        print(f"[validate_legs] Summary: {result.get('summary', 'N/A')}")
        print(f"[validate_legs] Closure: {result.get('closure_assessment', 'N/A')}")
        print(f"[validate_legs] Total issues: {result.get('total_issues', 0)}")

    # Build a lookup from leg_num to validation result
    validation_by_num = {}
    for vleg in result.get("legs", []):
        validation_by_num[vleg["leg_num"]] = vleg

    # Apply flags and confidence to each leg
    for leg in legs:
        leg_num = leg.get("leg_num")
        vleg = validation_by_num.get(leg_num, {})
        leg["flags"] = vleg.get("flags", [])
        leg["confidence"] = vleg.get("confidence", 0.5)

    if verbose:
        flagged = [l for l in legs if l.get("flags")]
        print(f"[validate_legs] {len(flagged)} leg(s) flagged with issues:")
        for leg in flagged:
            print(f"  Leg {leg['leg_num']}: confidence={leg['confidence']:.2f}")
            for flag in leg["flags"]:
                print(f"    - {flag}")

    return legs


if __name__ == "__main__":
    # Load a sample legs.json and validate it
    if len(sys.argv) > 1:
        with open(sys.argv[1], "r") as f:
            legs = json.load(f)
        # If a second argument is provided, treat it as the raw text file
        raw_text = ""
        if len(sys.argv) > 2:
            with open(sys.argv[2], "r") as f:
                raw_text = f.read()
        validated = validate_legs(legs, raw_text, verbose=True)
        print(f"\nValidated {len(validated)} legs.")
        print(json.dumps(validated, indent=2))
    else:
        print("Usage: python validate_legs.py <legs.json> [raw_text.txt]")
        print("  legs.json   - JSON file with list of leg dicts")
        print("  raw_text.txt - (optional) raw legal description text")
        sys.exit(1)
