import os, re, subprocess
from pathlib import Path
import json
DOWNLOAD_DIR = Path(__file__).parent.parent / 'downloads'
with open(Path(__file__).parent / 'styles.json', encoding='utf-8') as f:
    SUBTITLE_STYLES = json.load(f)
with open(Path(__file__).parent / 'animations.json', encoding='utf-8') as f:
    SUBTITLE_ANIMATIONS = json.load(f)


def ts_to_ms(ts: str) -> int:
    """Convert subtitle timestamp (00:00:01,500 or 00:00:01.500) to milliseconds."""
    ts = ts.strip().replace(",", ".")
    parts = ts.split(":")
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3_600_000 + int(m) * 60_000 + int(float(s) * 1000)
    elif len(parts) == 2:
        m, s = parts
        return int(m) * 60_000 + int(float(s) * 1000)
    return 0


def ms_to_ass(ms: int) -> str:
    """Convert milliseconds → ASS timestamp H:MM:SS.cc"""
    cs = ms // 10
    h  = cs // 360_000
    m  = (cs % 360_000) // 6_000
    s  = (cs % 6_000) // 100
    c  = cs % 100
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def parse_subtitles(path: Path, word_by_word: bool = False) -> list:
    """
    Parse SRT or VTT subtitle file.
    Returns list of (start_ms, end_ms, text) tuples.
    Handles YouTube rolling VTT auto-caption format with deduplication.
    """
    content = path.read_text(encoding="utf-8", errors="replace")
    content = content.lstrip("\ufeff")  # strip BOM

    events = []
    blocks = re.split(r"\n{2,}", content.strip())

    for block in blocks:
        lines = block.strip().splitlines()
        if not lines:
            continue

        # Find the timestamp line
        ts_line = None
        ts_idx = 0
        for i, line in enumerate(lines):
            if "-->" in line:
                ts_line = line
                ts_idx = i
                break
        if ts_line is None:
            continue

        # Parse timestamps
        m = re.match(
            r"(\d{1,2}:\d{2}:\d{2}[.,]\d{2,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[.,]\d{2,3})",
            ts_line,
        )
        if not m:
            continue

        start_ms = ts_to_ms(m.group(1))
        end_ms   = ts_to_ms(m.group(2))

        # Collect text lines after the timestamp
        raw = " ".join(lines[ts_idx + 1:])

        # Strip YouTube timing tags like <00:00:01.320>
        raw = re.sub(r"<\d{2}:\d{2}:\d{2}[.,]\d{3}>", "", raw)
        # Strip <c>, </c>, <b>, </b>, <i>, </i>  and other HTML-like tags
        raw = re.sub(r"</?[a-zA-Z][^>]*>", "", raw)
        # Strip VTT positioning cues on timestamp line (already handled)
        raw = raw.strip()

        # Skip blank or zero-duration entries
        if raw and start_ms < end_ms:
            events.append((start_ms, end_ms, raw))

    # Deduplicate rolling captions: drop consecutive identical text
    deduped = []
    prev = None
    for ev in events:
        if ev[2] != prev:
            deduped.append(ev)
            prev = ev[2]
            
    # Fix overlapping timestamps (common in YouTube auto-generated VTTs)
    # If an event overlaps with the next one, truncate its end time.
    fixed_events = []
    for i in range(len(deduped)):
        start_ms, end_ms, text = deduped[i]
        
        if i + 1 < len(deduped):
            next_start = deduped[i+1][0]
            if end_ms > next_start:
                end_ms = next_start
                
        if end_ms > start_ms:
            fixed_events.append((start_ms, end_ms, text))

    if not word_by_word:
        return fixed_events
        
    word_events = []
    prev_words = []
    
    for start_ms, end_ms, text in fixed_events:
        curr_words = text.split()
        if not curr_words:
            continue
            
        # Deduplicate overlapping words from rolling captions
        max_overlap = 0
        for i in range(1, min(len(prev_words), len(curr_words)) + 1):
            if prev_words[-i:] == curr_words[:i]:
                max_overlap = i
                
        new_words = curr_words[max_overlap:]
        if not new_words:
            prev_words = curr_words
            continue
            
        total_len = sum(len(w) for w in new_words)
        total_dur = end_ms - start_ms
        
        current_start = start_ms
        for i, w in enumerate(new_words):
            word_dur = int((len(w) / total_len) * total_dur) if total_len > 0 else 0
            word_end = current_start + word_dur
            
            # Ensure the last word extends precisely to the end_ms to avoid rounding gaps
            if i == len(new_words) - 1:
                word_end = end_ms
                
            if word_end > current_start:
                word_events.append((current_start, word_end, w))
            current_start = word_end
            
        prev_words = curr_words
            
    return word_events


def generate_ass(events: list[tuple[int, int, str]], style_key: str, anim_key: str, sub_color: str = "") -> str:
    """
    Build a full .ass file string from subtitle events using the chosen style.
    PlayRes is fixed at 1920x1080; FFmpeg scales to actual video resolution.
    """
    s = dict(SUBTITLE_STYLES.get(style_key, SUBTITLE_STYLES.get("classic", {})))
    anim = SUBTITLE_ANIMATIONS.get(anim_key, SUBTITLE_ANIMATIONS.get("normal", {}))

    if sub_color and sub_color.startswith("#") and len(sub_color) == 7:
        r, g, b = sub_color[1:3], sub_color[3:5], sub_color[5:7]
        s["primary_color"] = f"&H00{b}{g}{r}"

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: None\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,"
        f"{s['fontname']},"
        f"{s['fontsize']},"
        f"{s['primary_color']},"
        f"{s['secondary_color']},"
        f"{s['outline_color']},"
        f"{s['back_color']},"
        f"{s['bold']},"
        f"{s['italic']},"
        f"0,0,100,100,"
        f"{s['spacing']},0,"
        f"{s['border_style']},"
        f"{s['outline']},"
        f"{s['shadow']},"
        f"{s['alignment']},"
        f"10,10,{s['margin_v']},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    dialogue_lines = []
    override = s.get("line_override", "") + anim.get("tag", "")
    for start_ms, end_ms, text in events:
        clean = text.replace("\n", r"\N").replace("\r", "")
        dialogue_lines.append(
            f"Dialogue: 0,"
            f"{ms_to_ass(start_ms)},"
            f"{ms_to_ass(end_ms)},"
            f"Default,,0,0,0,,{override}{clean}"
        )

    return header + "\n".join(dialogue_lines) + "\n"


# ── Download helpers ──────────────────────────────────────────────────────────

def _fmt_bytes(b: float) -> str:
    if not b:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.2f} TB"


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()


def find_subtitle_file(base_title: str, lang: str) -> Path | None:
    for ext in ("srt", "vtt", "ass"):
        for p in [
            DOWNLOAD_DIR / f"{base_title}.{lang}.{ext}",
            DOWNLOAD_DIR / f"{base_title}.{ext}",
        ]:
            if p.exists():
                return p
    # glob fallback
    for ext in ("srt", "vtt"):
        found = list(DOWNLOAD_DIR.glob(f"*.{lang}.{ext}")) or list(DOWNLOAD_DIR.glob(f"*.{ext}"))
        if found:
            return found[0]
    return None


def burn_subtitles(video_path: Path, sub_path: Path, style_key: str, anim_key: str, sub_color: str, record: dict, aspect_ratio: str = "original", word_by_word: bool = False) -> Path:
    """Parse subs → generate styled ASS → FFmpeg burn into video."""
    record["status"] = "parsing_subs"
    record["status_label"] = "Parsing subtitles…"

    events = parse_subtitles(sub_path, word_by_word=word_by_word)
    if not events:
        raise RuntimeError("No subtitle events found in the downloaded subtitle file.")

    record["status_label"] = f"Generating ASS ({len(events)} lines)…"

    # Write styled ASS file
    ass_content = generate_ass(events, style_key, anim_key, sub_color)
    ass_path = video_path.with_suffix(".ass")
    ass_path.write_text(ass_content, encoding="utf-8")

    output_path = video_path.with_stem(video_path.stem + "_subbed")

    record["status"] = "burning"
    record["status_label"] = f"Burning '{SUBTITLE_STYLES.get(style_key, {}).get('label', style_key)}' style…"

    # FFmpeg: burn ASS into video frames
    # On Windows, subtitle filter path needs forward slashes + escaped colons
    ass_str = str(ass_path).replace("\\", "/").replace(":", "\\:")
    fonts_dir = Path(__file__).parent / "fonts"
    fonts_dir_str = str(fonts_dir).replace("\\", "/").replace(":", "\\:")

    vf_filters = []
    if aspect_ratio == "9:16":
        vf_filters.append("crop=ih*9/16:ih")
    elif aspect_ratio == "16:9":
        vf_filters.append("crop=iw:iw*9/16")
        
    vf_filters.append(f"ass='{ass_str}':fontsdir='{fonts_dir_str}'")
    vf_string = ",".join(vf_filters)

    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-vf", vf_string,
        "-c:a", "copy",
        "-sn",
        "-preset", "fast",
        "-crf", "20",
        str(output_path),
    ]

    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )

    # Clean up ASS file
    ass_path.unlink(missing_ok=True)

    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed:\n{result.stderr[-600:]}")

    return output_path


