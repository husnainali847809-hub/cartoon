import os
import random
import requests
from urllib.parse import quote
from moviepy import VideoFileClip

# ==================== CONFIGURATION ====================
BUFFER_ACCESS_TOKEN = os.getenv("BUFFER_ACCESS_TOKEN")
BUFFER_PROFILE_ID = os.getenv("BUFFER_PROFILE_ID")

ARCHIVE_SEARCH_URL = "https://archive.org/advancedsearch.php"
ARCHIVE_METADATA_URL = "https://archive.org/metadata/{}"
ARCHIVE_DOWNLOAD_URL = "https://archive.org/download/{}/{}"

DOWNLOAD_OUTPUT = "downloaded_video.mp4"
SHORTS_OUTPUT = "final_cartoon_video.mp4"

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


def get_viral_cartoon_url():
    """Find a downloadable cartoon video on Internet Archive."""
    print("[*] Searching Internet Archive for cartoon videos...")
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
        try:
            response = requests.get(ARCHIVE_METADATA_URL.format(quote(identifier)), timeout=30)
            response.raise_for_status()
            data = response.json()
            metadata = data.get("metadata", {})
            runtime = parse_runtime(metadata.get("runtime"))
            if runtime is not None and not 60 <= runtime <= 900:
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
            rights_info = metadata.get("licenseurl") or metadata.get("rights") or "Not specified in Archive.org metadata"

            print(f"[+] Video found: {title}")
            print(f"[+] Rights metadata: {rights_info}")
            print(f"[+] Source: {source_page}")
            return video_url, title, source_page, str(creator), str(rights_info)

        except (requests.RequestException, ValueError, TypeError) as exc:
            print(f"[!] Skipping Archive.org item {identifier}: {exc}")
            continue

    raise RuntimeError(
        "Internet Archive par is waqt suitable downloadable MP4 nahi mila. Agli scheduled run par dobara try hoga."
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


def convert_to_vertical_short(input_path, output_path, duration=120):
    print(f"[*] Processing video to 9:16 format ({duration} seconds)...")
    clip = VideoFileClip(input_path)

    start_time = 10 if clip.duration > duration + 10 else 0
    end_time = min(clip.duration, start_time + duration)
    clip = clip.subclipped(start_time, end_time)

    original_w, original_h = clip.size
    target_w = min(original_w, int(original_h * (9 / 16)))
    if target_w < 2:
        clip.close()
        raise RuntimeError("Source video dimensions are invalid.")

    cropped_clip = clip.cropped(
        x_center=original_w / 2,
        y_center=original_h / 2,
        width=target_w,
        height=original_h,
    )
    cropped_clip.write_videofile(
        output_path, codec="libx264", audio_codec="aac", fps=30
    )
    clip.close()
    cropped_clip.close()
    print("[+] Video processing complete!")


def upload_to_temp_host(file_path):
    """Upload the finished video to Litterbox for 24 hours and return its public URL."""
    import time

    print("[*] Uploading video to Litterbox (24-hour temporary link)...")
    upload_url = "https://litterbox.catbox.moe/resources/api.php"
    last_error = None

    for attempt in range(1, 4):
        try:
            with open(file_path, "rb") as file_handle:
                response = requests.post(
                    upload_url,
                    data={"reqtype": "fileupload", "time": "24h"},
                    files={"fileToUpload": file_handle},
                    timeout=(30, 300),
                )

            if response.status_code == 200:
                public_url = response.text.strip()
                if public_url.startswith("https://litter.catbox.moe/"):
                    print(f"[+] Public Video URL: {public_url}")
                    print("[+] Temporary video link created (24-hour availability).")
                    return public_url
                raise RuntimeError(
                    f"Litterbox returned an invalid upload response: {public_url[:500]}"
                )

            body = response.text.strip().replace("\\n", " ")[:500]
            raise RuntimeError(
                f"Litterbox HTTP {response.status_code}: {body or response.reason}"
            )

        except (requests.RequestException, OSError, RuntimeError) as exc:
            last_error = exc
            print(f"[!] Public video upload attempt {attempt}/3 failed: {exc}")
            if attempt < 3:
                time.sleep(10 * attempt)

    raise RuntimeError(f"Public video upload failed after 3 attempts: {last_error}")

def send_to_buffer(video_url, caption):
    print("[*] Sending post to Buffer Queue...")
    url = "https://api.bufferapp.com/1/updates/create.json"
    payload = {
        "profile_ids[]": [BUFFER_PROFILE_ID],
        "text": caption,
        "shorten": "false",
        "now": "false",
        "media[video]": video_url,
    }
    headers = {"Authorization": f"Bearer {BUFFER_ACCESS_TOKEN}"}
    response = requests.post(url, data=payload, headers=headers, timeout=60)
    response.raise_for_status()
    result = response.json()

    if result.get("success") or result.get("updates"):
        print("[++++] Video successfully Buffer queue mein add ho gayi hai.")
        return
    raise RuntimeError(f"Buffer ne post accept nahi ki: {result}")


if __name__ == "__main__":
    try:
        if not BUFFER_ACCESS_TOKEN or not BUFFER_PROFILE_ID:
            raise RuntimeError("Missing Buffer Access Token or Profile ID in GitHub Secrets.")

        target_video_url, video_title, source_page, creator, license_name = get_viral_cartoon_url()
        download_video(target_video_url, DOWNLOAD_OUTPUT)
        convert_to_vertical_short(DOWNLOAD_OUTPUT, SHORTS_OUTPUT, duration=120)
        public_video_url = upload_to_temp_host(SHORTS_OUTPUT)

        video_caption = (
            f"✨ {video_title} ✨\n"
            f"Source: {source_page}\n"
            f"Creator: {creator}\n"
            f"License: {license_name}\n"
            "#Cartoons #Animation #Reels"
        )
        send_to_buffer(public_video_url, video_caption)

        for path in (DOWNLOAD_OUTPUT, SHORTS_OUTPUT):
            if os.path.exists(path):
                os.remove(path)
        print("[+] Automation task finished successfully.")

    except Exception as exc:
        print(f"[-] Automation failed: {exc}")
        raise
