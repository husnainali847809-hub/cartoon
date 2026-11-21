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

DOWNLOAD_TEMPLATE = "downloaded_video.%(ext)s"
# Source accounts: the original account plus the two newly requested accounts.
TIKTOK_PROFILE_URLS = [
    "https://www.tiktok.com/@cartoon.tv763",
    "https://www.tiktok.com/@cartoonenemation",
    "https://www.tiktok.com/@oggyandthecockroaches",
]
SHORTS_OUTPUT = "final_cartoon_video.mp4"
STATE_FILE = "processed_cartoon_sources.json"
MAX_PART_SECONDS = int(os.getenv("MAX_PART_SECONDS", "300"))  # Never make an episode part longer than 5 minutes
SHORT_SECONDS = int(os.getenv("SHORT_SECONDS", "60"))
MAX_SOURCE_SECONDS = int(os.getenv("MAX_SOURCE_SECONDS", "14400"))

# =======================================================


def get_viral_cartoon_url(seen_sources=None):
    """Choose the most popular unprocessed cartoon video across all TikTok accounts.

    Upload age is deliberately NOT used as a filter: older and newer videos
    compete together. Popularity is ranked primarily by views, then likes and
    comments. Videos already posted (or already waiting in the pending queue)
    are excluded to prevent duplicates.
    """
    from yt_dlp import YoutubeDL

    seen_sources = set(seen_sources or [])
    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
    }
    candidates = []
    account_errors = []

    for profile_url in TIKTOK_PROFILE_URLS:
        print(f"[*] Scanning cartoon profile for popular videos: {profile_url}")
        try:
            with YoutubeDL(options) as ydl:
                profile = ydl.extract_info(profile_url, download=False)

            username = profile_url.rstrip("/").split("@")[-1]
            entries = profile.get("entries") or []
            for entry in entries:
                if not entry:
                    continue
                video_id = str(entry.get("id") or "").strip()
                video_url = entry.get("webpage_url") or entry.get("url")
                if not video_url and video_id:
                    video_url = f"https://www.tiktok.com/@{username}/video/{video_id}"
                if not video_url or video_url in seen_sources:
                    continue

                views = entry.get("view_count")
                likes = entry.get("like_count")
                comments = entry.get("comment_count")

                # Flat playlist extraction may omit engagement statistics.
                # Fetch metadata for that individual candidate (without downloading)
                # so popularity is based on actual TikTok metrics, not upload date.
                if views is None:
                    try:
                        with YoutubeDL(options) as ydl:
                            details = ydl.extract_info(video_url, download=False)
                        views = details.get("view_count", 0)
                        likes = details.get("like_count", likes)
                        comments = details.get("comment_count", comments)
                        entry = {**entry, **details}
                    except Exception as exc:
                        print(f"[!] Could not read engagement stats for {video_url}: {exc}")
                        continue

                views = int(views or 0)
                likes = int(likes or 0)
                comments = int(comments or 0)
                if views <= 0:
                    print(f"[*] Skipping video without usable view statistics: {video_url}")
                    continue

                # Views determine popularity; likes/comments break close ties.
                candidates.append({
                    "url": video_url,
                    "title": str(entry.get("title") or entry.get("description") or "TikTok Cartoon"),
                    "creator": str(entry.get("uploader") or entry.get("channel") or username),
                    "views": views,
                    "likes": likes,
                    "comments": comments,
                    "score": (views * 1_000_000) + (likes * 100) + comments,
                })
        except Exception as exc:
            message = f"{profile_url}: {exc}"
            account_errors.append(message)
            print(f"[!] Could not scan this profile; continuing to the next. {message}")

    if not candidates:
        details = " | ".join(account_errors)
        raise RuntimeError(
            "Teeno TikTok accounts par koi unprocessed video usable view counts ke sath nahi mili."
            + (f" Account errors: {details}" if details else "")
        )

    candidates.sort(
        key=lambda item: (item["views"], item["likes"], item["comments"]),
        reverse=True,
    )
    chosen = candidates[0]
    print(
        f"[TRENDING PICK] {chosen['title']} | "
        f"Views: {chosen['views']:,} | Likes: {chosen['likes']:,} | "
        f"Comments: {chosen['comments']:,}"
    )
    print(f"[+] Selected from {chosen['creator']}: {chosen['url']}")
    return chosen["url"], chosen["title"], chosen["url"], chosen["creator"]

def download_video(url, output_path=None):
    """Download the TikTok source in its best available single-file format.

    No source-format conversion or remux is requested here. The original
    downloaded extension is retained; the existing editor creates its usual
    final MP4 output later in the pipeline.
    """
    from yt_dlp import YoutubeDL
    import glob

    print(f"[*] Downloading original TikTok video format: {url}")
    for old_file in glob.glob("downloaded_video.*"):
        try:
            os.remove(old_file)
        except OSError:
            pass

    options = {
        "format": "best",
        "outtmpl": DOWNLOAD_TEMPLATE,
        "noplaylist": True,
        "quiet": False,
        "no_warnings": False,
        "retries": 3,
        "fragment_retries": 3,
    }
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        downloaded_path = ydl.prepare_filename(info)

    if not os.path.exists(downloaded_path) or os.path.getsize(downloaded_path) < 100_000:
        raise RuntimeError("TikTok video download missing, empty, ya incomplete hai.")
    print(f"[+] Original source downloaded: {downloaded_path}")
    return downloaded_path

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
        # Background fills the complete portrait canvas; foreground stays
        # uncropped and is enlarged to the full 1080px width.
        filter_graph = (
            "[0:v]split=2[bgsrc][fgsrc];"
            "[bgsrc]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,boxblur=28:12,eq=brightness=-0.22:saturation=0.75[bg];"
            "[fgsrc]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2[vout]"
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
        print("[*] Fast FFmpeg render without part-number overlay: "
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

        print("[+] Clean clip verified: "
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
        json.dump({"sources": sorted(sources)}, handle, indent=2)


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
    source_path = download_video(target_video_url)

    source = VideoFileClip(source_path)
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
        # Production mode: render and schedule up to five videos in Buffer daily.
        pakistan_tz = ZoneInfo("Asia/Karachi")
        now = datetime.now(pakistan_tz)
        publish_slots = get_today_publish_slots(now)
        daily_target = min(5, len(publish_slots))

        if daily_target == 0:
            print("[INFO] Aaj ke tamam publishing slots guzar chuke hain. Pending videos kal schedule hongi.")
            raise SystemExit(0)

        seen_sources = load_processed_sources()
        pending_parts = load_pending_parts()
        print("[LIVE MODE] Buffer posting ENABLED.")
        print(f"[*] Pakistan time: {now.isoformat()}")
        print(f"[*] Future publishing slots available today: {len(publish_slots)}")
        print(f"[*] Existing pending episode parts: {len(pending_parts)}")

        # Pending parts are not marked as processed. They remain eligible until
        # every part has been successfully queued; this set only prevents adding
        # the same source episode to the backlog twice at the same time.
        reserved_sources = set(seen_sources) | {
            item.get("source_page") for item in pending_parts if item.get("source_page")
        }

        while len(pending_parts) < daily_target:
            before_count = len(pending_parts)
            source_page = build_episode_parts(reserved_sources, pending_parts)
            if len(pending_parts) <= before_count:
                raise RuntimeError("A new episode did not add any publishable parts.")
            reserved_sources.add(source_page)
            print(f"[+] Added {len(pending_parts) - before_count} parts. Backlog: {len(pending_parts)}")

        import glob
        cached_source_url = None
        cached_source_path = None
        scheduled_count = 0

        for task in pending_parts[:daily_target]:
            source_url = task["video_url"]
            if cached_source_url != source_url or not cached_source_path or not os.path.exists(cached_source_path):
                cached_source_path = download_video(source_url)
                cached_source_url = source_url

            output_name = (
                f"cartoon_part_{int(task['part_number']):02d}_of_"
                f"{int(task['total_parts']):02d}.mp4"
            )
            segment_duration = float(task["end"]) - float(task["start"])
            convert_to_vertical_short(
                cached_source_path,
                output_name,
                start_time=float(task["start"]),
                duration=segment_duration,
                part_number=int(task["part_number"]),
                total_parts=int(task["total_parts"]),
            )

            # Upload and verify the media URL before creating the Buffer post.
            media_url = upload_to_temp_host(output_name)
            cartoon_name = task.get("title") or "Cartoon"
            import re
            cartoon_name = re.sub(r"(?i)\bpart\s*[-:#]?\s*\d+(?:\s*(?:/|of)\s*\d+)?\b", "", cartoon_name)
            cartoon_name = re.sub(r"(?i)\b(ep|episode)\s*[-:#]?\s*\d+\b", "", cartoon_name)
            cartoon_name = re.sub(r"(?i)\b(source|credit)\s*[:：].*$", "", cartoon_name)
            cartoon_name = re.sub(r"#\w+", "", cartoon_name)
            cartoon_name = re.sub(r"\s+", " ", cartoon_name).strip(" -–—|:·") or "Cartoon"
            if "oggyandthecockroaches" in str(task.get("creator", "")).lower():
                cartoon_name = "Oggy and the Cockroaches"
            caption = (
                f"{cartoon_name}\n"
                "#Cartoon #CartoonShorts #CartoonClips #Animation #FunnyCartoon #Shorts"
            )
            slot = publish_slots[scheduled_count]
            due_at = slot.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

            post_id = send_to_buffer(
                media_url=media_url,
                caption=caption,
                video_title=cartoon_name,
                post_type="reel",
                due_at=due_at,
                media_type="video",
            )
            print(f"[BUFFER CONFIRMED] {post_id} scheduled for {slot.isoformat()}")

            # Remove a part only after Buffer confirms that it was queued.
            pending_parts.pop(0)
            save_pending_parts(pending_parts)
            scheduled_count += 1

            source_page = task.get("source_page")
            if source_page and not any(
                item.get("source_page") == source_page for item in pending_parts
            ):
                save_processed_source(source_page)
                print(f"[+] All parts of source episode marked processed: {source_page}")

        for downloaded_file in glob.glob("downloaded_video.*"):
            try:
                os.remove(downloaded_file)
            except OSError:
                pass
        for output_file in glob.glob("cartoon_part_*_of_*.mp4"):
            try:
                os.remove(output_file)
            except OSError:
                pass

        print(
            f"[SUCCESS] {scheduled_count} video(s) successfully added to Buffer "
            "at the selected Pakistan-time slots. Remaining parts stay queued for the next run."
        )
    except SystemExit:
        raise
    except Exception as exc:
        print(f"[-] Cartoon automation failed: {exc}")
        raise
