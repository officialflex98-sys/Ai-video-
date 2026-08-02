"""
Scene-script -> video assembler.

Pipeline:
  1. parse_scene_script()   reads script.txt into Scene objects. Handles:
                              (A) one-field-per-line format
                              (B) tab or pipe-delimited rows, including
                                  full Markdown tables
  2. attach_voiceover()     loads the single voiceover.wav produced by
                            generate_voiceover.py (if present) and scales
                            every scene's duration proportionally so the
                            total matches the real narration length
  3. fetch_scene_image()    generates ONE free AI thematic image per scene
                            via Pollinations.ai, based only on the scene's
                            generic keywords/description - never a specific
                            film's actual characters or scenes
  4. render_scene_to_file() builds ONE scene (its image animated with a
                            slow Ken Burns zoom/pan, plus its caption),
                            writes it to its own silent video file on disk,
                            then closes everything it opened. Scenes are
                            rendered one at a time so memory stays low
                            regardless of script length.
  5. assemble_video()       joins all per-scene files with ffmpeg's concat
                            demuxer (stream copy, minimal memory), then
                            lays the narration track and background music
                            over the joined result in one final encode pass

Requires: pip install moviepy requests python-dotenv
          ffmpeg on PATH (used directly for scene concatenation)
Env vars: SUPABASE_URL / SUPABASE_KEY (optional - logging only)

Note: image generation uses Pollinations.ai, a free, unauthenticated,
no-API-key public service. Being free and unauthenticated, it can be
slower or occasionally unavailable compared to a paid API - fetch_scene_image()
retries with backoff to absorb that.
"""

import os
import re
import time
import random
import shutil
import subprocess
import requests
from dataclasses import dataclass, field
from typing import List, Optional

from moviepy import (
    ImageClip,
    VideoFileClip,
    concatenate_videoclips,
    CompositeVideoClip,
    TextClip,
    AudioFileClip,
    CompositeAudioClip,
    afx,
)

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
SCRIPT_PATH = "script.txt"
OUTPUT_PATH = "final_video.mp4"
TARGET_RESOLUTION = (1920, 1080)

SCENE_RENDER_DIR = "scene_renders"
SCENE_IMAGE_DIR = "scene_images"

CAPTION_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

VOICEOVER_PATH = "voiceover.wav"

_DEFAULT_MUSIC = "music.mp3"
MUSIC_PATH: Optional[str] = _DEFAULT_MUSIC if os.path.exists(_DEFAULT_MUSIC) else None
MUSIC_VOLUME = 0.15
VOICEOVER_VOLUME = 1.0

# Pollinations.ai - free, no API key, no billing. Unauthenticated public
# service, so retries with backoff are used to absorb occasional slowness.
POLLINATIONS_URL = "https://image.pollinations.ai/prompt/{prompt}"
IMAGE_STYLE_SUFFIX = "cinematic lighting, highly detailed, professional photography, 4k"
IMAGE_MAX_RETRIES = 4
IMAGE_RETRY_BASE_DELAY = 8   # seconds, doubles each attempt
IMAGE_REQUEST_TIMEOUT = 90   # generation can be slow on a free public service

ZOOM_RATIO = 0.18   # how much the image zooms in over the full scene duration

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")


# ----------------------------------------------------------------------------
# Data model
# ----------------------------------------------------------------------------
@dataclass
class Scene:
    start: float
    end: float
    description: str
    keywords: List[str]
    voiceover: str = ""
    image_path: Optional[str] = None
    _duration_override: Optional[float] = None

    @property
    def duration(self) -> float:
        if self._duration_override is not None:
            return self._duration_override
        return self.end - self.start


# ----------------------------------------------------------------------------
# 1. Parsing
# ----------------------------------------------------------------------------
_TIMESTAMP_RE = re.compile(r"(\d+):(\d+)\s*[–-]\s*(\d+):(\d+)")


def _to_seconds(mm: str, ss: str) -> float:
    return int(mm) * 60 + int(ss)


def _split_keywords(cell: str) -> List[str]:
    parts = [p.strip().strip('"').strip("'") for p in cell.split(",")]
    return [p for p in parts if p]


def _detect_delimiter(line: str) -> Optional[str]:
    if "\t" in line:
        return "\t"
    if "|" in line:
        return "|"
    return None


def _is_markdown_separator(line: str) -> bool:
    stripped = line.strip().strip("|")
    return bool(stripped) and all(c in "-: \t" for c in stripped)


def _is_timestamp_only_line(line: str) -> bool:
    return bool(re.fullmatch(r"\d+:\d+\s*[–-]\s*\d+:\d+", line.strip()))


def parse_scene_script(path: str = SCRIPT_PATH) -> List[Scene]:
    with open(path, "r", encoding="utf-8") as f:
        raw_lines = [l.rstrip("\n") for l in f if l.strip()]

    if not raw_lines:
        return []

    scenes: List[Scene] = []

    if any(_is_timestamp_only_line(l) for l in raw_lines):
        i = 0
        n = len(raw_lines)
        while i < n:
            if _is_timestamp_only_line(raw_lines[i]):
                m = _TIMESTAMP_RE.search(raw_lines[i])
                start = _to_seconds(m.group(1), m.group(2))
                end = _to_seconds(m.group(3), m.group(4))
                description = raw_lines[i + 1] if i + 1 < n else ""
                keywords_raw = raw_lines[i + 2] if i + 2 < n else ""
                voiceover = raw_lines[i + 3] if i + 3 < n else ""
                scenes.append(Scene(
                    start=start, end=end, description=description,
                    keywords=_split_keywords(keywords_raw), voiceover=voiceover,
                ))
                i += 4
            else:
                i += 1
        return scenes

    delimiter = _detect_delimiter(raw_lines[0])

    for line in raw_lines:
        if _is_markdown_separator(line):
            continue

        if delimiter == "|":
            trimmed = line.strip()
            if trimmed.startswith("|"):
                trimmed = trimmed[1:]
            if trimmed.endswith("|"):
                trimmed = trimmed[:-1]
            cols = [c.strip() for c in trimmed.split("|")]
        elif delimiter:
            cols = [c.strip() for c in line.split(delimiter)]
        else:
            cols = [c.strip() for c in re.split(r"\s{2,}", line)]

        if len(cols) < 3:
            continue

        if not _TIMESTAMP_RE.search(cols[0]):
            continue

        m = _TIMESTAMP_RE.search(cols[0])
        start = _to_seconds(m.group(1), m.group(2))
        end = _to_seconds(m.group(3), m.group(4))

        description = cols[1]
        keywords = _split_keywords(cols[2])
        voiceover = cols[3] if len(cols) > 3 else ""

        scenes.append(Scene(start=start, end=end, description=description,
                             keywords=keywords, voiceover=voiceover))

    return scenes


# ----------------------------------------------------------------------------
# 2. Attaching the single combined voiceover (drives real duration)
# ----------------------------------------------------------------------------
def attach_voiceover(scenes: List[Scene], voiceover_path: str = VOICEOVER_PATH) -> Optional[str]:
    if not os.path.exists(voiceover_path):
        print(f"  [warn] no voiceover file found at {voiceover_path}, "
              f"using raw script timestamp durations")
        return None

    clip = AudioFileClip(voiceover_path)
    total_audio_duration = clip.duration
    clip.close()

    total_script_duration = sum(s.duration for s in scenes)
    if total_script_duration <= 0:
        print("  [warn] scenes have zero total scripted duration, "
              "skipping voiceover-based scaling")
        return None

    scale = total_audio_duration / total_script_duration
    for s in scenes:
        s._duration_override = s.duration * scale

    print(f"  Scaled {len(scenes)} scene(s) to match {total_audio_duration:.1f}s "
          f"of narration (scale factor {scale:.3f})")
    return voiceover_path


# ----------------------------------------------------------------------------
# 3. Generating a free AI thematic image per scene (Pollinations.ai)
# ----------------------------------------------------------------------------
def fetch_scene_image(scene: Scene, index: int, out_dir: str = SCENE_IMAGE_DIR) -> Optional[str]:
    """Generates ONE thematic image for a scene from its generic keywords/
    description only - never a specific film's characters or scenes, since
    script.txt's keywords are themselves written to stay generic. Retries
    with backoff since Pollinations.ai is a free, unauthenticated public
    service that can be slow or briefly unavailable."""
    os.makedirs(out_dir, exist_ok=True)
    dest = os.path.join(out_dir, f"scene_{index:03d}.jpg")

    prompt_base = ", ".join(scene.keywords) if scene.keywords else scene.description
    prompt = f"{prompt_base}, {IMAGE_STYLE_SUFFIX}"
    encoded_prompt = requests.utils.quote(prompt)
    url = POLLINATIONS_URL.format(prompt=encoded_prompt)
    params = {"width": TARGET_RESOLUTION[0], "height": TARGET_RESOLUTION[1], "nologo": "true"}

    for attempt in range(1, IMAGE_MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=IMAGE_REQUEST_TIMEOUT)
            resp.raise_for_status()
            with open(dest, "wb") as f:
                f.write(resp.content)
            return dest
        except requests.RequestException as e:
            wait = IMAGE_RETRY_BASE_DELAY * attempt
            print(f"    [retry] image generation failed ({e.__class__.__name__}), "
                  f"retrying in {wait}s (attempt {attempt}/{IMAGE_MAX_RETRIES})...")
            time.sleep(wait)

    print(f"  [warn] failed to generate image for scene {index} after "
          f"{IMAGE_MAX_RETRIES} attempts, scene will be skipped")
    return None


# ----------------------------------------------------------------------------
# 4. Rendering each scene to its own file (image + Ken Burns + caption)
# ----------------------------------------------------------------------------
def _make_caption(text: str, duration: float, target_resolution=TARGET_RESOLUTION):
    return (
        TextClip(
            text=text,
            font=CAPTION_FONT_PATH,
            font_size=42,
            color="white",
            method="caption",
            size=(int(target_resolution[0] * 0.8), None),
            margin=(0, 60),
        )
        .with_position(("center", "bottom"))
        .with_duration(duration)
    )


def _ken_burns_clip(image_path: str, duration: float,
                     target_resolution=TARGET_RESOLUTION, zoom_ratio: float = ZOOM_RATIO):
    """Animates a still image with a slow zoom-in over its full on-screen
    duration - the standard 'Ken Burns effect' - so a static AI-generated
    image reads as cinematic motion rather than a frozen slide."""
    base = ImageClip(image_path)

    w, h = target_resolution
    base_w, base_h = base.size
    scale_to_cover = max(w / base_w, h / base_h) * (1 + zoom_ratio)
    base = base.resized(scale_to_cover).with_duration(duration)

    def zoom(t):
        progress = t / duration if duration > 0 else 0
        return 1 + zoom_ratio * progress

    animated = base.resized(zoom).with_position("center")
    return CompositeVideoClip([animated], size=target_resolution).with_duration(duration)


def render_scene_to_file(image_path: str, duration: float, caption_text: str,
                          out_path: str, fps: int = 30,
                          target_resolution=TARGET_RESOLUTION) -> str:
    if not image_path:
        raise ValueError("No image available to build scene")

    opened = []
    try:
        scene_clip = _ken_burns_clip(image_path, duration, target_resolution)
        opened.append(scene_clip)

        caption = _make_caption(caption_text, duration, target_resolution)
        opened.append(caption)

        composite = CompositeVideoClip([scene_clip, caption])
        opened.append(composite)

        composite.write_videofile(
            out_path, fps=fps, codec="libx264", audio=False, logger=None,
        )
    finally:
        for clip in opened:
            try:
                clip.close()
            except Exception:
                pass

    return out_path


# ----------------------------------------------------------------------------
# 5. Joining per-scene files (ffmpeg concat demuxer - stream copy)
# ----------------------------------------------------------------------------
def _ffmpeg_concat(scene_files: List[str], output_path: str) -> str:
    list_path = output_path + ".concat_list.txt"
    with open(list_path, "w") as f:
        for path in scene_files:
            escaped = os.path.abspath(path).replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")

    base_cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path]
    try:
        subprocess.run(base_cmd + ["-c", "copy", output_path],
                        check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        stderr_tail = e.stderr[-300:] if e.stderr else "unknown error"
        print(f"  [warn] stream-copy concat failed, retrying with re-encode: {stderr_tail}")
        subprocess.run(base_cmd + ["-c:v", "libx264", "-pix_fmt", "yuv420p", output_path],
                        check=True, capture_output=True, text=True)
    finally:
        os.remove(list_path)

    return output_path


# ----------------------------------------------------------------------------
# 6. Assembling the final video
# ----------------------------------------------------------------------------
def assemble_video(scenes: List[Scene], voiceover_path: Optional[str] = None,
                    music_path: Optional[str] = MUSIC_PATH,
                    output_path: str = OUTPUT_PATH,
                    scene_render_dir: str = SCENE_RENDER_DIR):
    if os.path.exists(scene_render_dir):
        shutil.rmtree(scene_render_dir)
    os.makedirs(scene_render_dir)

    scene_files = []
    for i, scene in enumerate(scenes):
        scene_path = os.path.join(scene_render_dir, f"scene_{i:03d}.mp4")
        print(f"  Rendering scene {i + 1}/{len(scenes)} ({scene.duration:.1f}s)...")
        render_scene_to_file(scene.image_path, scene.duration, scene.description, scene_path)
        scene_files.append(scene_path)

    silent_path = os.path.join(scene_render_dir, "_concatenated_silent.mp4")
    print(f"  Joining {len(scene_files)} scene(s) (stream copy)...")
    _ffmpeg_concat(scene_files, silent_path)

    video = VideoFileClip(silent_path)
    audio_tracks = []

    if voiceover_path and os.path.exists(voiceover_path):
        narration = AudioFileClip(voiceover_path).with_effects(
            [afx.MultiplyVolume(VOICEOVER_VOLUME)]
        )
        if narration.duration > video.duration:
            narration = narration.subclipped(0, video.duration)
        audio_tracks.append(narration)
    else:
        print("  [warn] no voiceover attached to final video")

    if music_path:
        music = AudioFileClip(music_path).with_effects(
            [afx.AudioLoop(duration=video.duration), afx.MultiplyVolume(MUSIC_VOLUME)]
        )
        audio_tracks.append(music)

    if audio_tracks:
        final_audio = audio_tracks[0] if len(audio_tracks) == 1 else CompositeAudioClip(audio_tracks)
        video = video.with_audio(final_audio)

    video.write_videofile(output_path, fps=30, codec="libx264", audio_codec="aac")
    video.close()

    shutil.rmtree(scene_render_dir, ignore_errors=True)

    return output_path


# ----------------------------------------------------------------------------
# Optional Supabase logging
# ----------------------------------------------------------------------------
def log_to_supabase(scenes: List[Scene], voiceover_path: Optional[str] = None):
    if not (SUPABASE_URL and SUPABASE_KEY):
        return
    try:
        payload = [
            {"start": s.start, "end": s.end, "duration": s.duration,
             "description": s.description, "keywords": s.keywords,
             "image": s.image_path, "voiceover": voiceover_path}
            for s in scenes
        ]
        requests.post(
            f"{SUPABASE_URL}/rest/v1/scene_runs",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                      "Content-Type": "application/json"},
            json=payload, timeout=15,
        )
    except requests.RequestException as e:
        print(f"[warn] Supabase logging skipped: {e}")


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------
def main():
    scenes = parse_scene_script(SCRIPT_PATH)
    print(f"Parsed {len(scenes)} scenes from {SCRIPT_PATH}")

    voiceover_path = attach_voiceover(scenes)

    for i, scene in enumerate(scenes):
        print(f"[{i + 1}/{len(scenes)}] Generating image for: {scene.description!r} "
              f"(duration {scene.duration:.1f}s)")
        scene.image_path = fetch_scene_image(scene, i)

    scenes = [s for s in scenes if s.image_path]

    log_to_supabase(scenes, voiceover_path)

    output = assemble_video(scenes, voiceover_path=voiceover_path)
    print(f"Done -> {output}")


if __name__ == "__main__":
    main()
