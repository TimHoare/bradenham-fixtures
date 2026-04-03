import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv

load_dotenv()

API_TOKEN = os.environ["API_TOKEN"]
SITE_ID = os.environ["SITE_ID"]
CLUB_NAME = "Bradenham"
SEASON = 2026
CALENDARS_DIR = Path("calendars")
BASE_URL = os.environ.get("BASE_URL", "")

# Manual overrides for grounds missing coordinates in the API
GROUND_OVERRIDES = {
    "UEA SportsPark": {"lat": "52.6245439", "lon": "1.2409256"},
    "South Walsham Playing Field": {"lat": "52.6636", "lon": "1.5040"},
}

SENIOR_TEAMS = {"1st XI", "Friendly XI", "Sunday 1st XI", "Fixture Secretary XI"}
SENIOR_DURATION = timedelta(hours=6)
DEFAULT_DURATION = timedelta(hours=2, minutes=30)


def fetch_fixtures():
    resp = requests.get(
        "http://play-cricket.com/api/v2/matches.json",
        params={"site_id": SITE_ID, "season": SEASON, "api_token": API_TOKEN},
    )
    resp.raise_for_status()
    return resp.json()["matches"]


def fetch_club_home_ground(club_id):
    """Fetch a club's most common home ground from their own fixtures.

    Returns a dict with ground_name, ground_latitude, ground_longitude.
    """
    resp = requests.get(
        "http://play-cricket.com/api/v2/matches.json",
        params={"site_id": club_id, "season": SEASON, "api_token": API_TOKEN},
    )
    if not resp.ok:
        return None
    matches = resp.json().get("matches", [])
    home_matches = [
        m
        for m in matches
        if str(m.get("home_club_id")) == str(club_id) and m.get("ground_name")
    ]
    if not home_matches:
        return None
    # Find most common ground
    most_common_name = Counter(m["ground_name"] for m in home_matches).most_common(1)[0][0]
    # Get coordinates from a match at that ground
    for m in home_matches:
        if m["ground_name"] == most_common_name:
            return {
                "ground_name": most_common_name,
                "ground_latitude": m.get("ground_latitude", ""),
                "ground_longitude": m.get("ground_longitude", ""),
            }
    return None


def build_ground_lookup(matches):
    """Build a club_id -> ground info mapping from matches that have ground data."""
    lookup = {}
    for m in matches:
        if m.get("ground_name") and m.get("home_club_id"):
            cid = m["home_club_id"]
            if cid not in lookup:
                lookup[cid] = {
                    "ground_name": m["ground_name"],
                    "ground_latitude": m.get("ground_latitude", ""),
                    "ground_longitude": m.get("ground_longitude", ""),
                }
    return lookup


def fill_missing_grounds(matches, ground_lookup):
    """Fill in missing ground names using the lookup, fetching from other clubs if needed."""
    missing_club_ids = set()
    for m in matches:
        if not m.get("ground_name") and m.get("home_club_id"):
            cid = m["home_club_id"]
            if cid in ground_lookup:
                m["ground_name"] = ground_lookup[cid]["ground_name"]
                m["ground_latitude"] = ground_lookup[cid]["ground_latitude"]
                m["ground_longitude"] = ground_lookup[cid]["ground_longitude"]
            else:
                missing_club_ids.add(cid)

    # Fetch grounds for clubs we couldn't resolve locally
    for cid in missing_club_ids:
        ground = fetch_club_home_ground(cid)
        if ground:
            ground_lookup[cid] = ground

    # Second pass with updated lookup
    still_missing = 0
    for m in matches:
        if not m.get("ground_name") and m.get("home_club_id"):
            cid = m["home_club_id"]
            if cid in ground_lookup:
                m["ground_name"] = ground_lookup[cid]["ground_name"]
                m["ground_latitude"] = ground_lookup[cid]["ground_latitude"]
                m["ground_longitude"] = ground_lookup[cid]["ground_longitude"]
            else:
                still_missing += 1

    return still_missing


def group_by_team(matches):
    teams = defaultdict(list)
    for match in matches:
        if str(match.get("home_club_id")) == SITE_ID:
            teams[match.get("home_team_name", "Unknown")].append(match)
        elif str(match.get("away_club_id")) == SITE_ID:
            teams[match.get("away_team_name", "Unknown")].append(match)
    return dict(teams)


def team_slug(team_name):
    return re.sub(r"[^a-z0-9]+", "-", team_name.lower()).strip("-")


def match_duration(match):
    home = match.get("home_team_name", "")
    away = match.get("away_team_name", "")
    if home in SENIOR_TEAMS or away in SENIOR_TEAMS:
        return SENIOR_DURATION
    return DEFAULT_DURATION


def match_summary(match):
    """Build a clear summary like 'Bradenham 1st XI vs Dereham 2nd XI'."""
    home_club = match.get("home_club_name", "").replace(" CC, Norfolk", "").replace(" CC", "")
    away_club = match.get("away_club_name", "").replace(" CC, Norfolk", "").replace(" CC", "")
    home_team = match.get("home_team_name", "Unknown")
    away_team = match.get("away_team_name", "Unknown")
    return f"{home_club} {home_team} vs {away_club} {away_team}"


def match_url(match):
    match_id = match.get("id", "")
    return f"https://play-cricket.com/website/results/{match_id}"


def match_coords(match):
    """Return (lat, lon) for a match, using API data or manual overrides."""
    ground = match.get("ground_name", "")
    lat = match.get("ground_latitude", "")
    lon = match.get("ground_longitude", "")
    if not lat or not lon:
        override = GROUND_OVERRIDES.get(ground)
        if override:
            lat, lon = override["lat"], override["lon"]
    return lat, lon


def match_location(match):
    """Build a location string that Google Calendar can map.

    Uses 'Ground Name (lat, lon)' which Google resolves to a map pin.
    Falls back to ground name alone if no coordinates are available.
    """
    ground = match.get("ground_name", "")
    lat, lon = match_coords(match)
    if lat and lon:
        return f"{ground} ({lat}, {lon})" if ground else f"{lat}, {lon}"
    return ground


def make_ical_event(match):
    match_date = match.get("match_date", "")
    match_time = match.get("match_time", "")
    match_id = match.get("id", "0")
    duration = match_duration(match)

    try:
        dt = datetime.strptime(match_date, "%d/%m/%Y")
    except ValueError:
        return None

    if match_time:
        try:
            time = datetime.strptime(match_time, "%H:%M").time()
            dt = dt.replace(hour=time.hour, minute=time.minute)
            dt_end = dt + duration
            dtstart = f"DTSTART:{dt.strftime('%Y%m%dT%H%M%S')}"
            dtend = f"DTEND:{dt_end.strftime('%Y%m%dT%H%M%S')}"
        except ValueError:
            dtstart = f"DTSTART;VALUE=DATE:{dt.strftime('%Y%m%d')}"
            dtend = None
    else:
        dtstart = f"DTSTART;VALUE=DATE:{dt.strftime('%Y%m%d')}"
        dtend = None

    summary = match_summary(match)
    location = match_location(match)
    competition = match.get("competition_name", "")
    url = match_url(match)
    description = f"Competition: {competition}\\n{url}" if competition else url
    uid = f"match-{match_id}@play-cricket.com"

    lat, lon = match_coords(match)

    lines = [
        "BEGIN:VEVENT",
        dtstart,
    ]
    if dtend:
        lines.append(dtend)
    lines += [
        f"SUMMARY:{summary}",
        f"LOCATION:{location}",
        f"UID:{uid}",
        f"URL:{url}",
        f"DESCRIPTION:{description}",
    ]
    if lat and lon:
        lines.append(f"GEO:{lat};{lon}")
    lines.append("END:VEVENT")
    return "\n".join(lines)


def write_calendar(team_name, matches):
    slug = team_slug(team_name)
    events = [make_ical_event(m) for m in matches]
    events = [e for e in events if e is not None]

    cal = "\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            f"PRODID:-//{CLUB_NAME} CC//Fixtures//EN",
            f"X-WR-CALNAME:{team_name} Fixtures {SEASON}",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
            *events,
            "END:VCALENDAR",
        ]
    )

    CALENDARS_DIR.mkdir(exist_ok=True)
    path = CALENDARS_DIR / f"{slug}.ics"
    path.write_text(cal)
    return path


def write_index_html(teams):
    team_rows = []
    for name in sorted(teams.keys()):
        slug = team_slug(name)
        count = len(teams[name])
        team_rows.append(
            {
                "name": name,
                "slug": slug,
                "count": count,
                "file": f"calendars/{slug}.ics",
            }
        )
    # Add all-teams entry
    total = sum(r["count"] for r in team_rows)
    team_rows.insert(
        0,
        {
            "name": f"{CLUB_NAME} All Teams",
            "slug": team_slug(f"{CLUB_NAME} All Teams"),
            "count": total,
            "file": f"calendars/{team_slug(f'{CLUB_NAME} All Teams')}.ics",
        },
    )

    teams_js = ",\n        ".join(
        f'{{ name: "{r["name"]}", file: "{r["file"]}", count: {r["count"]} }}'
        for r in team_rows
    )

    html = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{CLUB_NAME} CC Fixtures {SEASON}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #f5f5f5;
            color: #333;
            padding: 2rem;
            max-width: 640px;
            margin: 0 auto;
        }}
        h1 {{
            font-size: 1.5rem;
            margin-bottom: 0.5rem;
        }}
        .subtitle {{
            color: #666;
            margin-bottom: 1.5rem;
        }}
        .team {{
            background: white;
            border-radius: 8px;
            padding: 1rem;
            margin-bottom: 0.5rem;
            display: flex;
            align-items: center;
            gap: 0.75rem;
            box-shadow: 0 1px 3px rgba(0,0,0,0.08);
        }}
        .team input[type="checkbox"] {{
            width: 1.2rem;
            height: 1.2rem;
            cursor: pointer;
        }}
        .team-info {{ flex: 1; }}
        .team-name {{ font-weight: 600; }}
        .team-count {{ color: #888; font-size: 0.85rem; }}
        .actions {{
            margin-top: 1.5rem;
            display: flex;
            flex-direction: column;
            gap: 0.75rem;
        }}
        button {{
            padding: 0.75rem 1.5rem;
            border: none;
            border-radius: 8px;
            font-size: 1rem;
            cursor: pointer;
            font-weight: 600;
        }}
        button:disabled {{
            opacity: 0.4;
            cursor: not-allowed;
        }}
        .btn-google {{
            background: #1a73e8;
            color: white;
        }}
        .btn-google:hover:not(:disabled) {{ background: #1557b0; }}
        .btn-download {{
            background: #e8e8e8;
            color: #333;
        }}
        .btn-download:hover:not(:disabled) {{ background: #d0d0d0; }}
        .help {{
            margin-top: 2rem;
            padding: 1rem;
            background: #e8f0fe;
            border-radius: 8px;
            font-size: 0.85rem;
            color: #555;
        }}
        .help summary {{
            cursor: pointer;
            font-weight: 600;
            color: #333;
        }}
        .all-teams {{ border-left: 3px solid #1a73e8; }}
    </style>
</head>
<body>
    <h1>{CLUB_NAME} CC Fixtures {SEASON}</h1>
    <p class="subtitle">Select teams to add to your Google Calendar</p>

    <div id="teams"></div>

    <div class="actions">
        <button class="btn-google" id="btn-subscribe" disabled onclick="subscribe()">
            Subscribe in Google Calendar
        </button>
        <button class="btn-download" id="btn-download" disabled onclick="download()">
            Download .ics files
        </button>
    </div>

    <details class="help">
        <summary>How does this work?</summary>
        <p style="margin-top: 0.5rem;">
            <strong>Subscribe</strong> adds a live calendar to Google Calendar that updates
            automatically when fixtures change. Each selected team is added as a separate calendar.
        </p>
        <p style="margin-top: 0.5rem;">
            <strong>Download</strong> gives you .ics files you can import into any calendar app.
            Imported events won't update automatically.
        </p>
    </details>

    <script>
        const BASE = window.location.href.replace(/\\/[^/]*$/, "/").replace(/\\/$/, "");
        const teams = [
        {teams_js}
        ];

        const container = document.getElementById("teams");
        teams.forEach((t, i) => {{
            const div = document.createElement("div");
            div.className = "team" + (i === 0 ? " all-teams" : "");
            div.innerHTML = `
                <input type="checkbox" id="team-${{i}}" value="${{i}}" onchange="updateButtons()">
                <label for="team-${{i}}" class="team-info">
                    <div class="team-name">${{t.name}}</div>
                    <div class="team-count">${{t.count}} fixtures</div>
                </label>
            `;
            container.appendChild(div);
        }});

        function getSelected() {{
            return [...document.querySelectorAll('input[type=checkbox]:checked')]
                .map(cb => teams[parseInt(cb.value)]);
        }}

        function updateButtons() {{
            const any = document.querySelectorAll('input[type=checkbox]:checked').length > 0;
            document.getElementById("btn-subscribe").disabled = !any;
            document.getElementById("btn-download").disabled = !any;
        }}

        function subscribe() {{
            const selected = getSelected();
            selected.forEach(t => {{
                const icsUrl = BASE + "/" + t.file;
                const webcalUrl = icsUrl.replace(/^https?:/, "webcal:");
                const googleUrl = "https://calendar.google.com/calendar/r?cid=" + encodeURIComponent(webcalUrl);
                window.open(googleUrl, "_blank");
            }});
        }}

        function download() {{
            const selected = getSelected();
            selected.forEach(t => {{
                const a = document.createElement("a");
                a.href = BASE + "/" + t.file;
                a.download = t.file.split("/").pop();
                document.body.appendChild(a);
                a.click();
                a.remove();
            }});
        }}
    </script>
</body>
</html>
"""
    Path("index.html").write_text(html)
    return Path("index.html")


def main():
    print(f"Fetching {CLUB_NAME} fixtures for {SEASON}...")
    matches = fetch_fixtures()
    print(f"Found {len(matches)} matches")

    # Fill in missing grounds
    ground_lookup = build_ground_lookup(matches)
    no_ground_before = sum(1 for m in matches if not m.get("ground_name"))
    print(f"\nMatches missing ground: {no_ground_before}")
    still_missing = fill_missing_grounds(matches, ground_lookup)
    print(f"Resolved: {no_ground_before - still_missing}, still missing: {still_missing}")

    teams = group_by_team(matches)
    if not teams:
        print("No matches found for the club")
        return

    print(f"\nTeams found: {len(teams)}")
    for team_name, team_matches in sorted(teams.items()):
        path = write_calendar(team_name, team_matches)
        print(f"  {team_name}: {len(team_matches)} fixtures -> {path}")

    # All teams combined calendar
    all_matches = [m for ms in teams.values() for m in ms]
    seen = set()
    unique = []
    for m in all_matches:
        if m["id"] not in seen:
            seen.add(m["id"])
            unique.append(m)
    path = write_calendar(f"{CLUB_NAME} All Teams", unique)
    print(f"  All teams: {len(unique)} fixtures -> {path}")

    # Generate the HTML page
    index = write_index_html(teams)
    print(f"\nGenerated {index}")
    print(
        "Push to GitHub and enable Pages to use the subscribe feature,\n"
        "or open index.html locally to download .ics files."
    )


if __name__ == "__main__":
    main()
