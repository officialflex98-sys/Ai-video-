"""
Fetches a single royalty-free background instrumental track from Jamendo
and saves it as ./music.mp3. Build_video.py automatically picks this file
up as background music if it exists.

Requires: pip install requests
Env vars: JAMENDO_CLIENT_ID (required - free at https://developer.jamendo.com)
"""

import os
import random
import requests

JAMENDO_CLIENT_ID = os.environ.get("JAMENDO_CLIENT_ID", "")
JAMENDO_SEARCH_URL = "https://api.jamendo.com/v3.0/tracks/"
OUTPUT_PATH = "music.mp3"

# Broad, mood-neutral tags that fit a documentary voiceover bed. If
# script.txt ever includes Music Style/Mood columns in the future, this
# could be extended to search those tags per-run instead.
SEARCH_TAGS = ["cinematic", "documentary", "ambient", "epic"]


def fetch_music(output_path: str = OUTPUT_PATH) -> str:
    if not JAMENDO_CLIENT_ID:
        print("  [warn] JAMENDO_CLIENT_ID not set, skipping background music")
        return ""

    tag = random.choice(SEARCH_TAGS)
    params = {
        "client_id": JAMENDO_CLIENT_ID,
        "format": "json",
        "limit": 10,
        "tags": tag,
        "audioformat": "mp3",
        "include": "musicinfo",
        "order": "popularity_total",
    }

    try:
        resp = requests.get(JAMENDO_SEARCH_URL, params=params, timeout=20)
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except requests.RequestException as e:
        print(f"  [warn] Jamendo search failed: {e}")
        return ""

    if not results:
        print(f"  [warn] no tracks found for tag '{tag}', skipping background music")
        return ""

    track = random.choice(results)
    audio_url = track.get("audio")
    if not audio_url:
        print("  [warn] selected track had no audio URL, skipping background music")
        return ""

    print(f"  Downloading '{track.get('name', 'unknown')}' by "
          f"{track.get('artist_name', 'unknown')} (tag: {tag})...")

    try:
        with requests.get(audio_url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(output_path, "wb") as fh:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    fh.write(chunk)
    except requests.RequestException as e:
        print(f"  [warn] download failed: {e}")
        return ""

    print(f"  Wrote {output_path}")
    return output_path


if __name__ == "__main__":
    fetch_music()
