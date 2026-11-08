import os
import json
import random
import requests
from urllib.parse import quote
from moviepy import VideoFileClip

# ==================== CONFIGURATION ====================
BUFFER_ACCESS_TOKEN = os.getenv("BUFFER_ACCESS_TOKEN")  # legacy fallback only
BUFFER_CHANNEL_ID = os.getenv("BUFFER_CHANNEL_ID")

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
    processed_duration = float(cropped_clip.duration)
    clip.close()
    cropped_clip.close()
    print(f"[+] Video processing complete! Duration: {processed_duration:.2f} seconds")
    return processed_duration


def upload_to_temp_host(file_path):
    """Publish a video through beacon.host's large-file API and return its public URL.

    Anonymous beacon.host uploads are public for 24 hours. The three-step API
    supports files up to 250 MB without an account.
    """
    import hashlib
    import mimetypes

    print("[*] Uploading video to beacon.host (large-file upload)...")
    file_size = os.path.getsize(file_path)
    max_size = 250 * 1024 * 1024
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


def send_to_buffer(video_url, caption, video_title, post_type):
    """Queue one Facebook video with an explicit Facebook post type."""
    print(f"[*] Sending Facebook {post_type} video to Buffer Queue...")
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
    safe_url = json.dumps(video_url)
    safe_title = json.dumps(video_title, ensure_ascii=False)

    query = f'''
    mutation CreateCartoonVideoPost {{
      createPost(input: {{
        text: {safe_text}
        channelId: "{channel_id}"
        schedulingType: automatic
        mode: addToQueue
        metadata: {{
          facebook: {{
            type: {post_type}
          }}
        }}
        assets: [
          {{
            video: {{
              url: {safe_url}
              metadata: {{ title: {safe_title} }}
            }}
          }}
        ]
      }}) {{
        ... on PostActionSuccess {{
          post {{
            id
            text
            status
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
            f"Post ID: {post['id']} | Status: {post.get('status', 'unknown')}"
        )
        return post["id"]

    message = result.get("message") or "Buffer did not return a successful post."
    raise RuntimeError(f"Buffer could not queue the Facebook {post_type}: {message}")


if __name__ == "__main__":
    try:
        if not (os.getenv("BUFFER_API_KEY") or BUFFER_ACCESS_TOKEN):
            raise RuntimeError("Missing Buffer API key. Add GitHub Secret BUFFER_API_KEY from Buffer Settings > API.")

        target_video_url, video_title, source_page, creator, license_name = get_viral_cartoon_url()
        download_video(target_video_url, DOWNLOAD_OUTPUT)
        processed_duration = convert_to_vertical_short(
            DOWNLOAD_OUTPUT, SHORTS_OUTPUT, duration=120
        )
        public_video_url = upload_to_temp_host(SHORTS_OUTPUT)

        video_caption = (
            f"✨ {video_title} ✨\n"
            f"Source: {source_page}\n"
            f"Creator: {creator}\n"
            f"License: {license_name}\n"
            "#Cartoons #Animation #Reels"
        )
        if processed_duration <= 60:
            print(
                f"[*] Short video detected ({processed_duration:.1f}s). "
                "Queueing both Facebook Story and Facebook Post."
            )
            # Queue separately so Buffer assigns each item its own next queue slot.
            send_to_buffer(public_video_url, video_caption, video_title, "story")
            send_to_buffer(public_video_url, video_caption, video_title, "post")
        else:
            print(
                f"[*] Video is longer than 60 seconds ({processed_duration:.1f}s). "
                "Queueing Facebook Video Post only."
            )
            send_to_buffer(public_video_url, video_caption, video_title, "post")

        for path in (DOWNLOAD_OUTPUT, SHORTS_OUTPUT):
            if os.path.exists(path):
                os.remove(path)
        print("[+] Automation task finished successfully.")

    except Exception as exc:
        print(f"[-] Automation failed: {exc}")
        raise
