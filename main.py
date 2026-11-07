import os
import requests
import yt_dlp
from moviepy import VideoFileClip

# ==================== CONFIGURATION ====================
BUFFER_ACCESS_TOKEN = os.getenv("BUFFER_ACCESS_TOKEN")
BUFFER_PROFILE_ID = os.getenv("BUFFER_PROFILE_ID")

# Cartoon Network India ka official channel link (Aap isse kisi bhi doosre channel se badal sakte hain)
CARTOON_CHANNEL_URL = "https://youtube.com"

DOWNLOAD_OUTPUT = "downloaded_video.mp4"
SHORTS_OUTPUT = "final_short.mp4"
# =======================================================

def get_latest_video_url(channel_url):
    """Channel se automatic sabse latest video ka URL nikalne ka function"""
    print(f"[*] Fetching the latest cartoon video from: {channel_url}")
    ydl_opts = {
        'extract_flat': True,
        'playlistend': 1, # Sirf sabse pehli (latest) video ka data chahiye
        'quiet': True
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        result = ydl.extract_info(channel_url, download=False)
        if 'entries' in result and len(result['entries']) > 0:
            latest_video = result['entries'][0]
            video_url = f"https://youtube.com{latest_video['id']}"
            print(f"[+] Found latest video title: {latest_video.get('title')}")
            print(f"[+] Direct URL: {video_url}")
            return video_url
        else:
            raise Exception("Channel se koi video nahi mil saki.")

def download_video(url, output_path):
    print(f"[*] Downloading video from: {url}")
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': output_path,
        'quiet': True
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    print("[+] Download complete!")

def convert_to_vertical_short(input_path, output_path, duration=45):
    print("[*] Processing video to vertical Shorts format...")
    clip = VideoFileClip(input_path)
    
    # Shuruati 5 seconds chor kar agle 45 seconds kaatna
    if clip.duration > duration + 5:
        clip = clip.subclip(5, duration + 5)
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
    print("[+] Shorts processing complete!")

def upload_to_temp_host(file_path):
    print("[*] Uploading video to temporary cloud storage for Buffer...")
    filename = os.path.basename(file_path)
    with open(file_path, 'rb') as f:
        response = requests.put(f"https://transfer.sh{filename}", data=f)
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
        'profile_ids': [BUFFER_PROFILE_ID],
        'text': caption,
        'shorten': 'false',
        'now': 'false',
        'media[video]': video_url
    }
    
    headers = {
        'Authorization': f'Bearer {BUFFER_ACCESS_TOKEN}',
        'Content-Type': 'application/x-www-form-urlencoded'
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
            
        # Step 1: Khud hi latest video ka pata lagao
        target_video_url = get_latest_video_url(CARTOON_CHANNEL_URL)
        
        # Step 2: Baki processing automatic hogi
        download_video(target_video_url, DOWNLOAD_OUTPUT)
        convert_to_vertical_short(DOWNLOAD_OUTPUT, SHORTS_OUTPUT, duration=45)
        public_video_url = upload_to_temp_host(SHORTS_OUTPUT)
        
        video_caption = "Latest Cartoon Fun! 🔥 Don't forget to follow for more daily shorts. #Cartoons #Reels #Shorts #Trending"
        send_to_buffer(public_video_url, video_caption)
        
        if os.path.exists(DOWNLOAD_OUTPUT): os.remove(DOWNLOAD_OUTPUT)
        if os.path.exists(SHORTS_OUTPUT): os.remove(SHORTS_OUTPUT)
        print("[+] Automation task finished successfully.")
        
    except Exception as e:
        print(f"[-] Error occurred: {str(e)}")
