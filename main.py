import os
import random
import requests
import yt_dlp
from moviepy import VideoFileClip

# ==================== CONFIGURATION ====================
BUFFER_ACCESS_TOKEN = os.getenv("BUFFER_ACCESS_TOKEN")
BUFFER_PROFILE_ID = os.getenv("BUFFER_PROFILE_ID")

# Viral aur famous cartoons ke automatic search keywords
CARTOON_KEYWORDS = [
    "tom and jerry classic full episodes cartoon",
    "mr bean cartoon full episodes funny",
    "oggy and the cockroaches latest episode",
    "looney tunes funny cartoon moments",
    "motu patlu new episodes hindi cartoon"
]

DOWNLOAD_OUTPUT = "downloaded_video.mp4"
SHORTS_OUTPUT = "final_cartoon_video.mp4"
# =======================================================

def get_viral_cartoon_url():
    """Keywords mein se random select karke 2 se 6 min ki viral video dhoondna"""
    selected_search = random.choice(CARTOON_KEYWORDS)
    print(f"[*] Searching YouTube for viral content: '{selected_search}'")
    
    ydl_opts = {
        'default_search': 'ytsearch',
        'extract_flat': True,
        'playlistend': 5,
        'quiet': True
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        search_query = f"ytsearch5:{selected_search}"
        result = ydl.extract_info(search_query, download=False)
        
        if 'entries' in result and len(result['entries']) > 0:
            # 1. Pehle duration matching video dhoondte hain
            for entry in result['entries']:
                if entry and isinstance(entry, dict):
                    duration = entry.get('duration', 0)
                    video_id = entry.get('id')
                    if video_id and 120 <= duration <= 360:
                        # FIXED: Sahi URL format laga diya
                        video_url = entry.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}"
                        print(f"[+] Found Viral Video: {entry.get('title')}")
                        print(f"[+] Video Duration: {int(duration/60)} min {int(duration%60)} sec")
                        print(f"[+] Direct URL: {video_url}")
                        return video_url, entry.get('title')
            
            # 2. Backup agar duration criteria match na ho
            top_entry = result['entries'][0]
            video_id = top_entry.get('id')
            # FIXED: Backup ke liye bhi sahi format lagaya
            video_url = entry.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}"
            print(f"[+] Backup Top Video Selected: {top_entry.get('title')}")
            print(f"[+] Direct URL: {video_url}")
            return video_url, top_entry.get('title')
        else:
            raise Exception("YouTube search se koi video nahi mil saki.")

def download_video(url, output_path):
    print(f"[*] Downloading video from: {url}")
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': output_path,
        'quiet': True,
        'nocheckcertificate': True
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    print("[+] Download complete!")

def convert_to_vertical_short(input_path, output_path, duration=120):
    print(f"[*] Processing video to 9:16 format ({duration} seconds)...")
    clip = VideoFileClip(input_path)
    
    start_time = 10
    if clip.duration > duration + start_time:
        clip = clip.subclip(start_time, duration + start_time)
    else:
        clip = clip.subclip(0, min(clip.duration, duration))
        
    original_w, original_h = clip.size
    target_w = int(original_h * (9 / 16))
    
    if target_w > original_w:
        target_w = original_w
        target_h = int(original_w * (16 / 9))
        clip = clip.resized(height=target_h)
        original_w, original_h = clip.size
    
    x_center = original_w / 2
    y_center = original_h / 2
    
    cropped_clip = clip.cropped(x_center=x_center, y_center=y_center, width=target_w, height=original_h)
    cropped_clip.write_videofile(output_path, codec="libx264", audio_codec="aac", fps=30)
    
    clip.close()
    cropped_clip.close()
    print("[+] Video processing complete!")

def upload_to_temp_host(file_path):
    print("[*] Uploading video to temporary cloud storage for Buffer...")
    filename = os.path.basename(file_path)
    url = f"https://bashupload.com{filename}"
    
    with open(file_path, 'rb') as f:
        response = requests.put(url, data=f)
        
    if response.status_code == 200:
        public_url = response.text.strip()
        print(f"[+] Public Video URL: {public_url}")
        return public_url
    else:
        raise Exception("Temporary cloud upload failed.")

def send_to_buffer(video_url, caption):
    print("[*] Sending post to Buffer Queue...")
    url = "https://bufferapp.com"
    
    payload = {
        'profile_ids[]': [BUFFER_PROFILE_ID],
        'text': caption,
        'shorten': 'false',
        'now': 'false',
        'media[video]': video_url
    }
    
    headers = {
        'Authorization': f'Bearer {BUFFER_ACCESS_TOKEN}'
    }
    
    response = requests.post(url, data=payload, headers=headers).json()
    
    if 'updates' in response or response.get('success') == True:
        print("[++++] Mubarak ho! Video successfully Buffer queue mein add ho gayi hai.")
    else:
        print(f"[-] Buffer Error: {response}")

if __name__ == "__main__":
    try:
        if not BUFFER_ACCESS_TOKEN or not BUFFER_PROFILE_ID:
            raise Exception("Missing Buffer Access Token or Profile ID in environment variables.")
            
        target_video_url, video_title = get_viral_cartoon_url()
        download_video(target_video_url, DOWNLOAD_OUTPUT)
        convert_to_vertical_short(DOWNLOAD_OUTPUT, SHORTS_OUTPUT, duration=120)
        public_video_url = upload_to_temp_host(SHORTS_OUTPUT)
        
        video_caption = f"✨ {video_title} ✨\nEnjoy this classic cartoon moment! #Cartoons #Reels"
        send_to_buffer(public_video_url, video_caption)
        
        if os.path.exists(DOWNLOAD_OUTPUT): os.remove(DOWNLOAD_OUTPUT)
        if os.path.exists(SHORTS_OUTPUT): os.remove(SHORTS_OUTPUT)
        print("[+] Automation task finished successfully.")
        
    except Exception as e:
        print(f"[-] Error occurred: {str(e)}")
