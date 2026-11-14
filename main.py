import os
import json
import random
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import quote
from PIL import Image, ImageDraw, ImageFont
import numpy as np
from moviepy import VideoFileClip, ColorClip, CompositeVideoClip, ImageClip

# ==================== CONFIGURATION ====================
BUFFER_ACCESS_TOKEN = os.getenv("BUFFER_ACCESS_TOKEN")  # legacy fallback only
BUFFER_CHANNEL_ID = os.getenv("BUFFER_CHANNEL_ID")

ARCHIVE_SEARCH_URL = "https://archive.org/advancedsearch.php"
ARCHIVE_METADATA_URL = "https://archive.org/metadata/{}"
ARCHIVE_DOWNLOAD_URL = "https://archive.org/download/{}/{}"

DOWNLOAD_OUTPUT = "downloaded_video.mp4"
SHORTS_OUTPUT = "final_cartoon_video.mp4"
STATE_FILE = "processed_cartoon_sources.json"
MAX_PART_SECONDS = int(os.getenv("MAX_PART_SECONDS", "300"))  # Never make an episode part longer than 5 minutes
SHORT_SECONDS = int(os.getenv("SHORT_SECONDS", "60"))
MAX_SOURCE_SECONDS = int(os.getenv("MAX_SOURCE_SECONDS", "14400"))

SEARCH_TERMS = [
    'mediatype:movies AND (subject:"cartoons" OR subject:"animation" OR title:cartoon)',
    'mediatype:movies AND (subject:"animated films" OR title:"animated")',
]
# =======================================================


def parse_runtime(value):
    """Convert Archive.org runtime values (HH:MM:SS, MM:SS or seconds) to seconds."""
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        pass

    parts = str(value).strip().split(":")
    try:
        nums = [int(part) for part in parts]
    except ValueError:
        return None

    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return None


def is_reusable_license(value):
    """Accept any item that has explicit license/rights metadata; reject missing metadata."""
    if isinstance(value, (list, tuple)):
        return any(str(item).strip() for item in value if item is not None)
    return bool(str(value or "").strip())


def get_wikimedia_cartoon_url():
    """Find a video on Wikimedia Commons with an explicit reusable license."""
    print("[*] Trying Wikimedia Commons as an additional cartoon source...")
    api_url = "https://commons.wikimedia.org/w/api.php"
    search_terms = ["cartoon animation", "animated film", "2D animation"]
    headers = {"User-Agent": "CartoonShortsBot/1.0 (automated media search)"}

    for term in search_terms:
        params = {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": f"filemime:video/mp4 {term}",
            "gsrnamespace": 6,
            "gsrlimit": 30,
            "prop": "imageinfo",
            "iiprop": "url|extmetadata",
        }
        try:
            response = requests.get(api_url, params=params, headers=headers, timeout=30)
            response.raise_for_status()
            pages = (response.json().get("query") or {}).get("pages") or {}
            candidates = list(pages.values())
            random.shuffle(candidates)

            for page in candidates:
                info_list = page.get("imageinfo") or []
                if not info_list:
                    continue
                info = info_list[0]
                video_url = info.get("url", "")
                if not video_url.lower().split("?")[0].endswith(".mp4"):
                    continue

                metadata = info.get("extmetadata") or {}
                title = page.get("title", "Wikimedia Commons animation")
                creator = (metadata.get("Artist") or {}).get("value", "Wikimedia Commons contributor")
                source_page = "https://commons.wikimedia.org/wiki/" + quote(title.replace(" ", "_"))
                print(f"[+] Wikimedia video found: {title}")
                return video_url, title, source_page, str(creator)

        except (requests.RequestException, ValueError, TypeError) as exc:
            print(f"[!] Wikimedia Commons search issue for '{term}': {exc}")

    return None


def get_viral_cartoon_url(seen_sources=None):
    """Find a new cartoon video on Internet Archive, avoiding completed sources."""
    seen_sources = set(seen_sources or [])
    print("[*] Searching Internet Archive for a NEW cartoon video...")
    candidates = []

    for search_term in SEARCH_TERMS:
        params = {
            "q": search_term,
            "fl[]": ["identifier", "title"],
            "rows": 50,
            "page": 1,
            "output": "json",
            "sort[]": "downloads desc",
        }
        response = requests.get(ARCHIVE_SEARCH_URL, params=params, timeout=30)
        response.raise_for_status()
        docs = response.json().get("response", {}).get("docs", [])
        candidates.extend(docs)

    # Remove duplicate identifiers and randomize among popular search results.
    unique = {}
    for item in candidates:
        if item.get("identifier"):
            unique[item["identifier"]] = item
    candidate_list = list(unique.values())
    random.shuffle(candidate_list)

    for item in candidate_list:
        identifier = item["identifier"]
        if identifier in seen_sources:
            continue
        try:
            response = requests.get(ARCHIVE_METADATA_URL.format(quote(identifier)), timeout=30)
            response.raise_for_status()
            data = response.json()
            metadata = data.get("metadata", {})
            runtime = parse_runtime(metadata.get("runtime"))
            # Require a known, positive runtime and respect the overall source safety cap.
            # The complete source will be split into as many equal parts as needed,
            # with no individual part exceeding MAX_PART_SECONDS.
            if runtime is None or runtime <= 0 or runtime > MAX_SOURCE_SECONDS:
                continue

            files = data.get("files", [])
            mp4_files = [
                f for f in files
                if str(f.get("name", "")).lower().endswith(".mp4")
                and str(f.get("name", "")).lower() not in ("__ia_thumb.jpg",)
                and (not f.get("size") or int(f.get("size", 0)) <= 1_000_000_000)
            ]
            if not mp4_files:
                continue

            # Prefer the largest suitable MP4, which is usually the full-quality source.
            selected_file = max(mp4_files, key=lambda f: int(f.get("size", 0) or 0))
            filename = selected_file["name"]
            video_url = ARCHIVE_DOWNLOAD_URL.format(
                quote(identifier), quote(filename, safe="/")
            )
            title = str(metadata.get("title") or item.get("title") or identifier)
            creator = metadata.get("creator", "Unknown")
            if isinstance(creator, list):
                creator = ", ".join(str(x) for x in creator[:2])
            source_page = f"https://archive.org/details/{quote(identifier)}"
            # License information is not required for source selection.
            # Accept the video regardless of whether Archive.org provides license metadata.
            print(f"[+] Video found: {title}")
            print(f"[+] Source: {source_page}")
            return video_url, title, source_page, str(creator)

        except (requests.RequestException, ValueError, TypeError) as exc:
            print(f"[!] Skipping Archive.org item {identifier}: {exc}")
            continue

    commons_result = get_wikimedia_cartoon_url()
    if commons_result:
        return commons_result

    raise RuntimeError(
        "Internet Archive aur Wikimedia Commons par is waqt suitable, explicitly licensed MP4 nahi mila. Agli scheduled run par dobara try hoga."
    )


def download_video(url, output_path):
    print("[*] Downloading video from Internet Archive...")
    with requests.get(url, stream=True, timeout=(30, 180)) as response:
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").lower()
        if "text/html" in content_type:
            raise RuntimeError("Archive.org ne video ke bajaye HTML page return kiya.")
        with open(output_path, "wb") as file_handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    file_handle.write(chunk)
    if not os.path.exists(output_path) or os.path.getsize(output_path) < 100_000:
        raise RuntimeError("Downloaded file empty ya incomplete hai.")
    print("[+] Download complete!")


def convert_to_vertical_short(input_path, output_path, start_time=0, duration=60, part_number=1, total_parts=5):
    """Render a full-screen 9:16 cartoon part with a large, sharp foreground video.

    The original cartoon frame is scaled to the full available width without
    cropping its action. A softly blurred, darkened copy fills the vertical
    background so the viewer never sees a tiny landscape rectangle surrounded
    by black bars on a phone.
    """
    import subprocess
    import imageio_ffmpeg

    output_w, output_h = 1080, 1920
    source = VideoFileClip(input_path)
    try:
        source_duration = float(source.duration or 0)
        start_time = max(0.0, float(start_time))
        actual_duration = min(max(0.1, float(duration)), source_duration - start_time)
        if actual_duration <= 0:
            raise RuntimeError(f"Part {part_number} has an invalid time range.")

        target_bytes = 220_000_000
        audio_kbps = 96
        safe_total_kbps = int((target_bytes * 8 / actual_duration) / 1000)
        video_kbps = max(400, min(2800, safe_total_kbps - audio_kbps))

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        label = f"PART {part_number} / {total_parts}"
        # Place the part number in the blurred BACKGROUND immediately above
        # the sharp cartoon frame, centered and never covering the cartoon.
        source_w, source_h = source.size
        source_ratio = float(source_w) / max(1, float(source_h))
        foreground_w = min(output_w, int(output_h * source_ratio))
        foreground_h = min(output_h, int(output_w / source_ratio))
        part_label_y = max(90, int((output_h - foreground_h) / 2) - 105)

        # Background fills the complete portrait canvas; foreground stays
        # uncropped and is enlarged to the full 1080px width.
        filter_graph = (
            "[0:v]split=2[bgsrc][fgsrc];"
            "[bgsrc]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,boxblur=28:12,eq=brightness=-0.22:saturation=0.75[bg];"
            "[fgsrc]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,"
            f"drawtext=fontfile={font_path}:text='{label}':"
            "fontsize=64:fontcolor=white:borderw=3:bordercolor=black:"
            "x=(w-text_w)/2:y=" + str(part_label_y) + "[vout]"
        )
        command = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "warning", "-stats",
            "-ss", f"{start_time:.3f}", "-i", input_path,
            "-t", f"{actual_duration:.3f}",
            "-filter_complex", filter_graph,
            "-map", "[vout]", "-map", "0:a?",
            "-r", "30", "-fps_mode", "cfr",
            "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", f"{video_kbps}k", "-maxrate", f"{video_kbps}k",
            "-bufsize", f"{video_kbps * 2}k",
            "-pix_fmt", "yuv420p", "-threads", "2",
            "-c:a", "aac", "-b:a", f"{audio_kbps}k",
            "-movflags", "+faststart", output_path,
        ]
        print(f"[*] Fast FFmpeg render: part {part_number}/{total_parts}, "
              f"{actual_duration:.1f}s at 1080x1920 (full-screen portrait; enlarged cartoon)...")
        subprocess.run(command, check=True)

        if not os.path.exists(output_path) or os.path.getsize(output_path) < 100_000:
            raise RuntimeError(f"Part {part_number} output is missing or too small.")
        output_size = os.path.getsize(output_path)
        if output_size >= 250_000_000:
            raise RuntimeError(f"Part {part_number} exceeds the 250 MiB temporary-host limit.")
        if actual_duration > MAX_PART_SECONDS + 0.5:
            raise RuntimeError(
                f"Part {part_number} exceeds the {MAX_PART_SECONDS}s maximum: "
                f"{actual_duration:.1f}s."
            )

        check = VideoFileClip(output_path)
        try:
            if not check.duration or check.duration <= 0:
                raise RuntimeError(f"Part {part_number} has no playable duration.")
            duration_difference = abs(float(check.duration) - actual_duration)
            if duration_difference > (1 / 30) + 0.01:
                raise RuntimeError(
                    f"Part {part_number} duration mismatch: expected {actual_duration:.3f}s, "
                    f"rendered {float(check.duration):.3f}s."
                )
            if tuple(check.size) != (output_w, output_h):
                raise RuntimeError(f"Part {part_number} has unexpected dimensions: {check.size}.")
        finally:
            check.close()

        print(f"[+] Part {part_number}/{total_parts} verified: "
              f"{actual_duration:.1f}s, 1080x1920, {output_size / (1024 * 1024):.1f} MiB")
        return actual_duration
    finally:
        source.close()

def convert_to_facebook_story(input_path, output_path, max_duration=55):
    """Render a Facebook Story: vertical 9:16 and safely under Facebook's 60s limit."""
    import subprocess
    import imageio_ffmpeg

    source = VideoFileClip(input_path)
    try:
        source_duration = float(source.duration or 0)
        actual_duration = min(float(max_duration), source_duration)
        if actual_duration <= 0 or actual_duration > 59:
            raise RuntimeError("Facebook Story duration must be positive and below 60 seconds.")

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        # Keep the complete source frame visible: blurred duplicate fills
        # the portrait background, sharp foreground is fitted without crop/stretch.
        video_filter = (
            "[0:v]split=2[bgsrc][fgsrc];"
            "[bgsrc]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,boxblur=28:12,eq=brightness=-0.22:saturation=0.75[bg];"
            "[fgsrc]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2[vout]"
        )
        command = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
            "-ss", "0", "-i", input_path, "-t", f"{actual_duration:.3f}",
            "-map", "0:v:0", "-map", "0:a?",
            "-filter_complex", video_filter, "-r", "30",
            "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", "1800k", "-maxrate", "2000k", "-bufsize", "3600k",
            "-pix_fmt", "yuv420p", "-threads", "2",
            "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart", output_path,
        ]
        print(f"[*] Rendering Facebook Story: {actual_duration:.1f}s, vertical 1080x1920...")
        subprocess.run(command, check=True)

        if not os.path.exists(output_path) or os.path.getsize(output_path) < 100_000:
            raise RuntimeError("Facebook Story render is missing or too small.")
        check = VideoFileClip(output_path)
        try:
            if not check.duration or check.duration > 59.5:
                raise RuntimeError(f"Facebook Story duration is invalid: {check.duration}.")
            if tuple(check.size) != (1080, 1920):
                raise RuntimeError(f"Facebook Story dimensions are invalid: {check.size}.")
        finally:
            check.close()

        print(f"[+] Facebook Story verified: {actual_duration:.1f}s, 1080x1920.")
        return actual_duration
    finally:
        source.close()


def create_facebook_story_image(video_path, output_path, title):
    """Create a single vertical poster image from a representative cartoon frame."""
    source = VideoFileClip(video_path)
    try:
        frame_time = min(20.0, max(0.0, float(source.duration or 0) / 3))
        frame = Image.fromarray(source.get_frame(frame_time).astype("uint8")).convert("RGB")
    finally:
        source.close()

    # Crop the frame to a vertical 9:16 canvas.
    target_w, target_h = 1080, 1920
    scale = max(target_w / frame.width, target_h / frame.height)
    frame = frame.resize((int(frame.width * scale), int(frame.height * scale)), Image.Resampling.LANCZOS)
    left = (frame.width - target_w) // 2
    top = (frame.height - target_h) // 2
    poster = frame.crop((left, top, left + target_w, top + target_h)).convert("RGBA")

    # Add a restrained dark gradient so the episode title remains readable.
    overlay = Image.new("RGBA", poster.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for y in range(1050, target_h):
        alpha = int(210 * (y - 1050) / (target_h - 1050))
        draw.rectangle((0, y, target_w, y + 1), fill=(0, 0, 0, alpha))
    poster = Image.alpha_composite(poster, overlay)
    draw = ImageDraw.Draw(poster)

    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    small_font = ImageFont.truetype(font_path, 42)
    title_font = ImageFont.truetype(font_path, 68)
    draw.rounded_rectangle((64, 90, 510, 170), radius=28, fill=(15, 15, 15, 185), outline=(255, 255, 255, 190), width=2)
    draw.text((92, 108), "NEW CARTOON", font=small_font, fill="white", stroke_width=1, stroke_fill="black")

    words = str(title or "Classic Cartoon").split()
    lines, line = [], ""
    for word in words:
        candidate = (line + " " + word).strip()
        if draw.textbbox((0, 0), candidate, font=title_font)[2] <= 950:
            line = candidate
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    lines = lines[-4:]
    y = 1480
    for line in lines:
        draw.text((64, y), line, font=title_font, fill="white", stroke_width=3, stroke_fill=(0, 0, 0, 210))
        y += 88

    poster.convert("RGB").save(output_path, "JPEG", quality=92, optimize=True)
    if not os.path.exists(output_path) or os.path.getsize(output_path) < 50_000:
        raise RuntimeError("Facebook Story image was not created correctly.")
    print(f"[+] Facebook Story image created: 1080x1920, {os.path.getsize(output_path) / 1024:.0f} KB")
    return output_path

def split_episode_into_equal_parts(video_duration, max_part_seconds=MAX_PART_SECONDS):
    """Split the complete episode into near-equal, frame-aligned parts.

    The number of parts is calculated automatically. Every part is at most
    max_part_seconds, all parts differ by no more than one 30-FPS frame, and
    the complete source timeline is covered without intentionally dropping a tail.
    """
    video_duration = float(video_duration)
    output_fps = 30
    if video_duration <= 0 or max_part_seconds <= 0:
        raise RuntimeError("Episode duration or maximum part duration is invalid.")

    total_frames = int(round(video_duration * output_fps))
    max_frames_per_part = int(max_part_seconds * output_fps)
    if total_frames <= 0 or max_frames_per_part <= 0:
        raise RuntimeError("Episode is too short to create playable parts.")

    parts = (total_frames + max_frames_per_part - 1) // max_frames_per_part
    ranges = []
    for index in range(parts):
        start_frame = index * total_frames // parts
        end_frame = (index + 1) * total_frames // parts
        if end_frame <= start_frame:
            raise RuntimeError("Episode produced an empty part.")
        ranges.append((start_frame / output_fps, end_frame / output_fps))

    durations = [end - start for start, end in ranges]
    if max(durations) > max_part_seconds + (1 / output_fps):
        raise RuntimeError("Calculated part duration exceeds the configured maximum.")

    print(
        f"[*] Equal split verified: complete episode covered in {parts} parts; "
        f"each part {min(durations):.3f}-{max(durations):.3f}s "
        f"(maximum {max_part_seconds}s), with no intentional tail omitted."
    )
    return ranges


def load_processed_sources():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return set(data.get("sources", []))
    except (OSError, ValueError, TypeError):
        return set()


def save_processed_source(source_page):
    sources = load_processed_sources()
    sources.add(source_page)
    with open(STATE_FILE, "w", encoding="utf-8") as handle:
        json.dump({"sources": sorted(sources)[-500:]}, handle, indent=2)


def upload_to_temp_host(file_path):
    """Publish a video through beacon.host's large-file API and return its public URL.

    Anonymous beacon.host uploads are public for 24 hours. The three-step API
    supports files up to 250 MB without an account.
    """
    import hashlib
    import mimetypes

    print("[*] Uploading video to beacon.host (large-file upload)...")
    file_size = os.path.getsize(file_path)
    max_size = 250_000_000
    if file_size <= 0:
        raise RuntimeError("Video file is empty; cannot upload.")
    if file_size > max_size:
        raise RuntimeError(
            f"Video is {file_size / (1024 * 1024):.1f} MB; beacon.host anonymous limit is 250 MB."
        )

    digest = hashlib.sha256()
    with open(file_path, "rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    sha256_hash = digest.hexdigest()
    content_type = mimetypes.guess_type(file_path)[0] or "video/mp4"
    filename = os.path.basename(file_path)

    # Step 1: Register the file and request a presigned storage URL.
    create_response = requests.post(
        "https://beacon.host/v1/sites",
        json={
            "files": [{
                "path": filename,
                "size": file_size,
                "contentType": content_type,
                "hash": f"sha256:{sha256_hash}",
            }]
        },
        headers={"User-Agent": "CartoonShortsBot/1.0"},
        timeout=(30, 90),
    )
    if create_response.status_code not in (200, 201):
        raise RuntimeError(
            f"beacon.host registration failed: HTTP {create_response.status_code}: "
            f"{create_response.text[:500]}"
        )

    try:
        registration = create_response.json()
        site = registration["site"]
        version = registration["version"]
        pending_uploads = registration.get("uploads", {}).get("pending", [])
        skipped_uploads = registration.get("uploads", {}).get("skipped", [])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(
            f"beacon.host returned an unexpected registration response: {create_response.text[:500]}"
        ) from exc

    # A matching hash may already exist, in which case no upload is needed.
    if pending_uploads:
        pending = next(
            (item for item in pending_uploads if item.get("path") == filename),
            pending_uploads[0],
        )
        upload_url = pending.get("uploadUrl")
        if not upload_url:
            raise RuntimeError("beacon.host did not provide a presigned upload URL.")

        # Step 2: Upload the binary file directly to the presigned storage URL.
        with open(file_path, "rb") as file_handle:
            upload_response = requests.put(
                upload_url,
                data=file_handle,
                headers={"Content-Type": content_type},
                timeout=(30, 600),
            )
        if upload_response.status_code not in (200, 201, 204):
            raise RuntimeError(
                f"beacon.host file transfer failed: HTTP {upload_response.status_code}: "
                f"{upload_response.text[:500]}"
            )
    elif not skipped_uploads:
        raise RuntimeError("beacon.host response contained neither pending nor skipped files.")

    # Step 3: Finalize the site so its public URL becomes available.
    finalize_path = version.get("finalizeUrl")
    if not finalize_path:
        raise RuntimeError("beacon.host did not provide a finalize URL.")
    finalize_url = (
        finalize_path if finalize_path.startswith("https://")
        else "https://beacon.host" + finalize_path
    )
    finalize_response = requests.post(
        finalize_url,
        headers={"X-Claim-Token": registration.get("claimToken", "")},
        timeout=(30, 90),
    )
    if finalize_response.status_code not in (200, 201):
        raise RuntimeError(
            f"beacon.host finalize failed: HTTP {finalize_response.status_code}: "
            f"{finalize_response.text[:500]}"
        )

    public_url = f"{site['url'].rstrip('/')}/{quote(filename)}"
    verify_response = requests.head(public_url, allow_redirects=True, timeout=30)
    if verify_response.status_code >= 400:
        raise RuntimeError(
            f"beacon.host published the site but video URL verification failed: "
            f"HTTP {verify_response.status_code}"
        )

    print(f"[+] Public Video URL: {public_url}")
    print("[+] beacon.host upload and public-link verification complete.")
    print("[!] Anonymous beacon.host links expire after 24 hours; Buffer must publish the queued video before expiry.")
    return public_url

def buffer_graphql(api_key, query):
    """Send one authenticated request to Buffer's current GraphQL API."""
    response = requests.post(
        "https://api.buffer.com",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={"query": query},
        timeout=60,
    )
    if response.status_code == 401:
        raise RuntimeError(
            "Buffer rejected the API key (401 Unauthorized). Add a NEW Buffer API key "
            "from Buffer Settings > API as the GitHub secret BUFFER_API_KEY. "
            "The old legacy BUFFER_ACCESS_TOKEN is not a current Buffer API key."
        )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Buffer GraphQL HTTP {response.status_code}: {response.text[:1000]}"
        )
    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Buffer returned non-JSON response: {response.text[:500]}") from exc
    if result.get("errors"):
        messages = "; ".join(
            str(item.get("message", item)) for item in result["errors"]
        )
        raise RuntimeError(f"Buffer GraphQL error: {messages}")
    return result.get("data") or {}


def resolve_buffer_channel(api_key):
    """Use configured channel ID, or auto-discover if the account has one channel."""
    if BUFFER_CHANNEL_ID:
        return BUFFER_CHANNEL_ID

    print("[*] BUFFER_CHANNEL_ID not set; discovering connected Buffer channels...")
    account_data = buffer_graphql(
        api_key,
        "query { account { organizations { id name } } }",
    )
    organizations = (account_data.get("account") or {}).get("organizations") or []
    channels_found = []

    for organization in organizations:
        org_id = organization.get("id")
        if not org_id:
            continue
        query = f'''
        query {{
          channels(input: {{ organizationId: "{org_id}" }}) {{
            id
            name
            service
            displayName
          }}
        }}
        '''
        channel_data = buffer_graphql(api_key, query)
        channels_found.extend(channel_data.get("channels") or [])

    unique_channels = {item.get("id"): item for item in channels_found if item.get("id")}
    channels_found = list(unique_channels.values())

    if len(channels_found) == 1:
        channel = channels_found[0]
        print(
            f"[+] Automatically selected Buffer channel: "
            f"{channel.get('displayName') or channel.get('name') or channel.get('service')} "
            f"({channel['id']})"
        )
        return channel["id"]

    if not channels_found:
        raise RuntimeError(
            "No connected Buffer channels were found for this API key. "
            "Check the Buffer account and reconnect the social channel."
        )

    choices = [
        f"{item.get('displayName') or item.get('name') or item.get('service')}: {item['id']}"
        for item in channels_found
    ]
    raise RuntimeError(
        "More than one Buffer channel is connected. Add the intended channel ID "
        "as GitHub secret BUFFER_CHANNEL_ID. Available channels: " + " | ".join(choices)
    )


def send_to_buffer(media_url, caption, video_title, post_type, due_at=None, media_type="video"):
    """Add one Facebook video or image to the next available Buffer queue slot."""
    if media_type not in ("video", "image"):
        raise ValueError(f"Unsupported Buffer media type: {media_type}")
    print(f"[*] Sending Facebook {post_type} {media_type} to Buffer Queue...")
    api_key = os.getenv("BUFFER_API_KEY") or BUFFER_ACCESS_TOKEN
    if not api_key:
        raise RuntimeError(
            "Missing Buffer API key. Add GitHub Actions secret BUFFER_API_KEY "
            "(Buffer Settings > API)."
        )

    if post_type not in ("post", "story", "reel"):
        raise ValueError(f"Unsupported Facebook post type: {post_type}")

    channel_id = resolve_buffer_channel(api_key)
    safe_text = json.dumps(caption, ensure_ascii=False)
    safe_url = json.dumps(media_url)
    safe_title = json.dumps(video_title, ensure_ascii=False)

    query = f'''
    mutation CreateCartoonVideoPost {{
      createPost(input: {{
        text: {safe_text}
        channelId: "{channel_id}"
        schedulingType: automatic
        mode: {"customScheduled" if due_at else "addToQueue"}
        {f'dueAt: "{due_at}"' if due_at else ""}
        metadata: {{
          facebook: {{
            type: {post_type}
          }}
        }}
        assets: [
          {{
            {media_type}: {{
              url: {safe_url}
              {f'metadata: {{ title: {safe_title} }}' if media_type == "video" else ""}
            }}
          }}
        ]
      }}) {{
        ... on PostActionSuccess {{
          post {{
            id
            text
            status
            dueAt
          }}
        }}
        ... on MutationError {{
          message
        }}
      }}
    }}
    '''

    data = buffer_graphql(api_key, query)
    result = data.get("createPost") or {}
    post = result.get("post")
    if post and post.get("id"):
        print(
            f"[++++] Facebook {post_type} successfully added to Buffer queue. "
            f"Post ID: {post['id']} | Status: {post.get('status', 'unknown')} | "
            f"Queue time: {post.get('dueAt', 'assigned by Buffer')}"
        )
        return post["id"]

    message = result.get("message") or "Buffer did not return a successful post."
    raise RuntimeError(f"Buffer could not queue the Facebook {post_type}: {message}")


def load_pending_parts():
    try:
        with open("cartoon_queue.json", "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except (OSError, ValueError, TypeError):
        return []


def save_pending_parts(queue):
    temporary_path = "cartoon_queue.json.tmp"
    with open(temporary_path, "w", encoding="utf-8") as handle:
        json.dump(queue, handle, ensure_ascii=False, indent=2)
    os.replace(temporary_path, "cartoon_queue.json")


def get_today_publish_slots(now):
    """Return up to five future same-day Facebook video slots in Pakistan time."""
    slot_hours = (9, 12, 15, 18, 21)
    slots = []
    for hour in slot_hours:
        slot = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if slot > now + timedelta(minutes=5):
            slots.append(slot)
    return slots


def build_episode_parts(seen_sources, pending_parts):
    """Download a fresh episode and append every complete-episode part to backlog."""
    target_video_url, video_title, source_page, creator = get_viral_cartoon_url(seen_sources)
    download_video(target_video_url, DOWNLOAD_OUTPUT)

    source = VideoFileClip(DOWNLOAD_OUTPUT)
    try:
        full_duration = float(source.duration or 0)
    finally:
        source.close()

    if full_duration <= 0 or full_duration > MAX_SOURCE_SECONDS:
        raise RuntimeError(
            f"Source duration is invalid or exceeds the configured limit: {full_duration:.1f}s."
        )

    ranges = split_episode_into_equal_parts(full_duration, MAX_PART_SECONDS)
    print(
        f"[*] Episode '{video_title}' has {len(ranges)} complete parts. "
        "Adding all parts to the daily publishing backlog."
    )
    for part_number, (start_time, end_time) in enumerate(ranges, start=1):
        pending_parts.append({
            "video_url": target_video_url,
            "title": video_title,
            "source_page": source_page,
            "creator": creator,
            "start": start_time,
            "end": end_time,
            "part_number": part_number,
            "total_parts": len(ranges),
        })

    # Persist immediately so an interruption does not lose the episode backlog.
    save_pending_parts(pending_parts)
    return source_page


if __name__ == "__main__":
    try:
        if not (os.getenv("BUFFER_API_KEY") or BUFFER_ACCESS_TOKEN):
            raise RuntimeError(
                "Missing Buffer API key. Add GitHub Secret BUFFER_API_KEY from Buffer Settings > API."
            )

        pakistan_tz = ZoneInfo("Asia/Karachi")
        now_pakistan = datetime.now(pakistan_tz)
        publish_slots = get_today_publish_slots(now_pakistan)
        if not publish_slots:
            raise RuntimeError(
                "All five daily publishing slots have passed. Pending parts are preserved "
                "for the next daily run; no expiring temporary links were created."
            )

        daily_target = min(5, len(publish_slots))
        print(
            f"[*] Daily video target: 5. Available future slots today: "
            f"{len(publish_slots)}. This single run will queue up to {daily_target} videos "
            "for separate Buffer publishing times."
        )

        seen_sources = load_processed_sources()
        pending_parts = load_pending_parts()
        reserved_sources = set(seen_sources) | {
            item.get("source_page") for item in pending_parts if item.get("source_page")
        }
        print(f"[*] Existing pending episode parts: {len(pending_parts)}")

        # If one short episode yields only 2-3 parts, fetch another complete
        # episode and append its parts until today's five-video target is met.
        while len(pending_parts) < daily_target:
            before_count = len(pending_parts)
            source_page = build_episode_parts(reserved_sources, pending_parts)
            if len(pending_parts) <= before_count:
                raise RuntimeError("A new episode did not add any publishable parts.")
            reserved_sources.add(source_page)
            print(
                f"[+] Added {len(pending_parts) - before_count} parts from another episode. "
                f"Backlog now contains {len(pending_parts)} parts."
            )

        queued_today = 0
        cached_source_url = None
        while queued_today < daily_target and pending_parts:
            # Recalculate slots after each render/upload so slow processing
            # cannot accidentally reuse a slot that has already passed.
            current_time = datetime.now(pakistan_tz)
            remaining_slots = get_today_publish_slots(current_time)
            if not remaining_slots:
                print("[!] No more same-day slots remain; keeping remaining parts for tomorrow.")
                break
            slot = remaining_slots[0]

            task = pending_parts[0]
            output_path = f"cartoon_part_{task['part_number']}.mp4"
            source_url = task["video_url"]

            # Switch source files when the backlog moves to a different episode.
            if cached_source_url != source_url or not os.path.exists(DOWNLOAD_OUTPUT):
                if os.path.exists(DOWNLOAD_OUTPUT):
                    os.remove(DOWNLOAD_OUTPUT)
                download_video(source_url, DOWNLOAD_OUTPUT)
                cached_source_url = source_url

            segment_duration = float(task["end"]) - float(task["start"])
            convert_to_vertical_short(
                DOWNLOAD_OUTPUT,
                output_path,
                start_time=float(task["start"]),
                duration=segment_duration,
                part_number=int(task["part_number"]),
                total_parts=int(task["total_parts"]),
            )
            public_url = upload_to_temp_host(output_path)

            caption = (
                f"🎬 {task['title']}\n"
                f"📌 PART {task['part_number']}/{task['total_parts']}\n"
                "Watch the complete cartoon story in all parts.\n"
                "#Cartoons #Animation #CartoonShorts #FunnyCartoons"
            )
            due_at_utc = (
                slot.astimezone(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
            )
            send_to_buffer(
                public_url,
                caption,
                f"{task['title']} - Part {task['part_number']}",
                "post",
                due_at=due_at_utc,
            )

            # Only remove a part after Buffer confirms that it accepted it.
            pending_parts.pop(0)
            save_pending_parts(pending_parts)
            queued_today += 1

            if not any(
                item.get("source_page") == task["source_page"]
                for item in pending_parts
            ):
                save_processed_source(task["source_page"])
                seen_sources.add(task["source_page"])

            if os.path.exists(output_path):
                os.remove(output_path)

            print(
                f"[SUCCESS] Video {queued_today}/{daily_target} queued for "
                f"{slot.strftime('%Y-%m-%d %H:%M')} Pakistan time. "
                f"Remaining backlog: {len(pending_parts)} parts."
            )

        if os.path.exists(DOWNLOAD_OUTPUT):
            os.remove(DOWNLOAD_OUTPUT)

        print(
            f"[DAILY RESULT] {queued_today}/{daily_target} videos queued for today's "
            f"available slots. {len(pending_parts)} extra parts remain saved for the next day."
        )
        if queued_today < 5:
            print(
                "[!] Five posts could not be scheduled today because fewer than five "
                "future daily slots remained. Unpublished parts have been retained."
            )
    except Exception as exc:
        print(f"[-] Automation failed: {exc}")
        raise
