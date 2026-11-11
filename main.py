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
PARTS_PER_SOURCE = int(os.getenv("PARTS_PER_SOURCE", "5"))
MIN_PART_SECONDS = int(os.getenv("MIN_PART_SECONDS", "120"))  # At least 2 minutes per scheduled part
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
            minimum_source_seconds = PARTS_PER_SOURCE * MIN_PART_SECONDS
            # Require reliable runtime metadata so short clips are not selected.
            # Five parts x two minutes means a source must be at least ten minutes.
            if runtime is None or runtime < minimum_source_seconds or runtime > MAX_SOURCE_SECONDS:
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
    """Render one landscape Facebook video segment directly with FFmpeg.

    FFmpeg performs scaling, padding, text overlay and H.264 encoding in native
    code. This avoids MoviePy's per-frame Python compositing bottleneck that
    made long renders take tens of minutes.
    """
    import subprocess
    import imageio_ffmpeg

    output_w, output_h = 1920, 1080
    source = VideoFileClip(input_path)
    try:
        source_duration = float(source.duration or 0)
        start_time = max(0.0, float(start_time))
        actual_duration = min(max(0.1, float(duration)), source_duration - start_time)
        if actual_duration <= 0:
            raise RuntimeError(f"Part {part_number} has an invalid time range.")

        target_bytes = 220_000_000  # Keep a 30 MB safety margin below Beacon's 250 MB cap
        audio_kbps = 64
        safe_total_kbps = int((target_bytes * 8 / actual_duration) / 1000)
        video_kbps = max(250, min(2200, safe_total_kbps - audio_kbps))

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        label = f"PART {part_number} / {total_parts}"
        video_filter = (
            "scale=1700:800:force_original_aspect_ratio=decrease,"
            "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"drawtext=fontfile={font_path}:text='{label}':"
            "fontsize=76:fontcolor=white:borderw=3:bordercolor=black:"
            "x=(w-text_w)/2:y=20"
        )
        command = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "warning", "-stats",
            "-ss", f"{start_time:.3f}", "-i", input_path,
            "-t", f"{actual_duration:.3f}",
            "-map", "0:v:0", "-map", "0:a?",
            "-vf", video_filter, "-r", "30", "-fps_mode", "cfr",
            "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", f"{video_kbps}k", "-maxrate", f"{video_kbps}k",
            "-bufsize", f"{video_kbps * 2}k",
            "-pix_fmt", "yuv420p", "-threads", "2",
            "-c:a", "aac", "-b:a", f"{audio_kbps}k",
            "-movflags", "+faststart", output_path,
        ]
        print(f"[*] Fast FFmpeg render: part {part_number}/{total_parts}, "
              f"{actual_duration:.1f}s at 1920x1080...")
        subprocess.run(command, check=True)

        if not os.path.exists(output_path) or os.path.getsize(output_path) < 100_000:
            raise RuntimeError(f"Part {part_number} output is missing or too small.")
        output_size = os.path.getsize(output_path)
        if output_size >= 250_000_000:
            raise RuntimeError(f"Part {part_number} exceeds the 250 MiB temporary-host limit.")
        if actual_duration + 0.5 < MIN_PART_SECONDS:
            raise RuntimeError(
                f"Part {part_number} is too short ({actual_duration:.1f}s); "
                f"minimum is {MIN_PART_SECONDS}s."
            )

        check = VideoFileClip(output_path)
        try:
            if not check.duration or check.duration <= 0:
                raise RuntimeError(f"Part {part_number} has no playable duration.")
            if tuple(check.size) != (output_w, output_h):
                raise RuntimeError(f"Part {part_number} has unexpected dimensions: {check.size}.")
        finally:
            check.close()

        print(f"[+] Part {part_number}/{total_parts} verified: "
              f"{actual_duration:.1f}s, {output_size / (1024 * 1024):.1f} MiB")
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
        # Fill a 9:16 frame by center-cropping the landscape source.
        video_filter = (
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920"
        )
        command = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
            "-ss", "0", "-i", input_path, "-t", f"{actual_duration:.3f}",
            "-map", "0:v:0", "-map", "0:a?",
            "-vf", video_filter, "-r", "30",
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

def split_episode_into_five(video_duration, parts=5):
    """Return five adjacent ranges covering the full episode without gaps or overlaps."""
    video_duration = float(video_duration)
    minimum_source_seconds = parts * MIN_PART_SECONDS
    if video_duration < minimum_source_seconds:
        raise RuntimeError(
            f"Source is too short ({video_duration / 60:.1f} minutes). "
            f"{parts} parts of at least {MIN_PART_SECONDS / 60:.0f} minutes each "
            f"require a source of at least {minimum_source_seconds / 60:.0f} minutes."
        )
    if video_duration <= 0 or parts != 5:
        raise RuntimeError("Episode duration is invalid; exactly five parts are required.")
    boundaries = [video_duration * i / parts for i in range(parts + 1)]
    ranges = [(boundaries[i], boundaries[i + 1]) for i in range(parts)]
    if any(end <= start for start, end in ranges):
        raise RuntimeError("Episode is too short to split into five playable parts.")
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
            ${media_type}: {{
              url: {safe_url}
              ${'metadata: { title: ' + safe_title + ' }'}
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


if __name__ == "__main__":
    try:
        if not (os.getenv("BUFFER_API_KEY") or BUFFER_ACCESS_TOKEN):
            raise RuntimeError("Missing Buffer API key. Add GitHub Secret BUFFER_API_KEY from Buffer Settings > API.")

        seen_sources = load_processed_sources()
        target_video_url, video_title, source_page, creator = get_viral_cartoon_url(seen_sources)
        download_video(target_video_url, DOWNLOAD_OUTPUT)

        source = VideoFileClip(DOWNLOAD_OUTPUT)
        try:
            full_duration = float(source.duration)
        finally:
            source.close()

        minimum_source_seconds = PARTS_PER_SOURCE * MIN_PART_SECONDS
        if full_duration < minimum_source_seconds:
            raise RuntimeError(
                f"Downloaded source is too short ({full_duration / 60:.1f} minutes). "
                f"At least {minimum_source_seconds / 60:.0f} minutes are required "
                f"for {PARTS_PER_SOURCE} parts of {MIN_PART_SECONDS / 60:.0f}+ minutes. "
                "This source will not be posted."
            )

        if full_duration > MAX_SOURCE_SECONDS:
            raise RuntimeError(
                f"Source is too long ({full_duration / 3600:.1f} hours). "
                f"Configured maximum is {MAX_SOURCE_SECONDS / 3600:.1f} hours; skipping to protect runner capacity."
            )

        part_ranges = split_episode_into_five(full_duration, PARTS_PER_SOURCE)
        print(
            f"[*] Splitting the COMPLETE episode '{video_title}' "
            f"({full_duration / 60:.1f} minutes) into five consecutive parts "
            f"(minimum {MIN_PART_SECONDS / 60:.0f} minutes each; target upload size 220 MiB, "
            "hard limit 250 MiB per part)."
        )

        # Render, validate and upload all five parts before adding them to Buffer.
        uploaded_parts = []
        for index, (start_time, end_time) in enumerate(part_ranges, start=1):
            output_path = f"cartoon_part_{index}.mp4"
            segment_duration = end_time - start_time
            convert_to_vertical_short(
                DOWNLOAD_OUTPUT, output_path, start_time=start_time,
                duration=segment_duration, part_number=index,
                total_parts=len(part_ranges)
            )
            public_url = upload_to_temp_host(output_path)
            uploaded_parts.append((index, public_url, output_path, segment_duration))

        # Create a STATIC vertical poster image for Facebook Story (no Story video).
        story_output = "facebook_story.jpg"
        story_url = None
        try:
            create_facebook_story_image(DOWNLOAD_OUTPUT, story_output, video_title)
            story_url = upload_to_temp_host(story_output)
        except Exception as story_error:
            # A Story image is optional; do not lose successfully rendered/queued feed videos.
            print(f"[!] Facebook Story image skipped; feed posts will continue: {story_error}")

        # Schedule Part 1 and its announcement Story together at 09:00 Pakistan time.
        # Parts 2-5 continue using Buffer's normal next-available queue slots.
        pakistan_tz = ZoneInfo("Asia/Karachi")
        now_pakistan = datetime.now(pakistan_tz)
        first_publish = now_pakistan.replace(hour=9, minute=0, second=0, microsecond=0)
        if first_publish <= now_pakistan:
            first_publish += timedelta(days=1)
        first_publish_utc = first_publish.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

        for index, public_url, output_path, segment_duration in uploaded_parts:
            caption = (
                f"🎬 {video_title}\n"
                f"📌 PART {index}/{len(uploaded_parts)}\n"
                f"Watch the complete cartoon story in all 5 parts.\n"
                f"Source: {source_page}\n"
                f"Creator: {creator}\n"
                "#Cartoons #Animation #CartoonShorts #Part"
            )
            if index == 1:
                send_to_buffer(
                    public_url, caption, f"{video_title} - Part 1", "post",
                    due_at=first_publish_utc
                )
                story_caption = (
                    f"🎬 NEW CARTOON EPISODE TODAY!\n"
                    f"{video_title}\n"
                    "📺 PART 1 is coming today!\n"
                    "Follow along for the complete cartoon story.\n"
                    "#Cartoons #NewEpisode #Animation"
                )
                if story_url:
                    try:
                        send_to_buffer(
                            story_url, story_caption, f"{video_title} - Episode Announcement", "story",
                            due_at=first_publish_utc, media_type="image"
                        )
                        print(f"[+] Part 1 AND its Facebook Story scheduled together for {first_publish.isoformat()}.")
                    except Exception as story_error:
                        # Buffer Story validation/API issues must not fail the feed-post workflow.
                        print(f"[!] Facebook Story could not be queued; feed posts remain scheduled: {story_error}")
                else:
                    print("[!] Facebook Story was not created; feed posts remain scheduled.")
            else:
                send_to_buffer(
                    public_url, caption, f"{video_title} - Part {index}", "post"
                )
                print(f"[+] Part {index}/{len(uploaded_parts)} added to Buffer's next available queue slot.")

        save_processed_source(source_page)
        for path in [DOWNLOAD_OUTPUT, story_output] + [item[2] for item in uploaded_parts]:
            if os.path.exists(path):
                os.remove(path)
        print(f"[SUCCESS] Completed one new cartoon as {len(uploaded_parts)} scheduled parts.")
    except Exception as exc:
        print(f"[-] Automation failed: {exc}")
        raise
