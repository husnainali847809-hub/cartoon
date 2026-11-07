import os
import requests
import yt_dlp
from moviepy.editor import VideoFileClip

# ==================== CONFIGURATION ====================
BUFFER_ACCESS_TOKEN = os.getenv("BUFFER_ACCESS_TOKEN")
BUFFER_PROFILE_ID = os.getenv("BUFFER_PROFILE_ID")
VIDEO_URL = "https://youtube.com"  # Sample URL (Aap isse change kar sakte hain)

DOWNLOAD_OUTPUT = "downloaded_video.mp4"
SHORTS_OUTPUT = "final_short.mp4"
# =======================================================

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
    
    if clip.duration > duration + 5:
        clip = clip.subclip(5, duration + 5)
    else:
        clip = clip.subclip(0, min(clip.duration, duration))
        
    original_w, original_h = clip.size
    target_w = int(original_h * (9 / 16))
    
    if target_w > original_w:
        target_w = original_w
        target_h = int(original_w * (16 / 9))
        clip = clip.resize(height=target_h)
        original_w, original_h = clip.size
    
    x_center = original_w / 2
    y_center = original_h / 2
    
    cropped_clip = clip.crop(x_center=x_center, y_center=y_center, width=target_w, height=original_h)
    cropped_clip.write_videofile(output_path, codec="libx264", audio_codec="aac", fps=30, logger=None)
    
    clip.close()
    cropped_clip.close()
    print("[+] Shorts processing complete!")

def upload_to_temp_host(file_path):
    """Buffer ko bhejane ke liye temporary public URL generate karna"""
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
    """Buffer API ke zariye post ko Facebook page ki queue mein bhej dena"""
    print("[*] Sending post to Buffer Queue...")
    url = "https://bufferapp.com"
    
    payload = {
        'profile_ids[]': [BUFFER_PROFILE_ID],
        'text': caption,
        'shorten': 'false',
        'now': 'false',  # True karne se foran post ho jayega, False se Queue mein jayega
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
            
        download_video(VIDEO_URL, DOWNLOAD_OUTPUT)
        convert_to_vertical_short(DOWNLOAD_OUTPUT, SHORTS_OUTPUT, duration=45)
        public_video_url = upload_to_temp_host(SHORTS_OUTPUT)
        
        video_caption = "Nostalgic Cartoon Moments! ✨ Don't forget to follow for more. #Cartoons #Reels #Shorts"
        send_to_buffer(public_video_url, video_caption)
        
        if os.path.exists(DOWNLOAD_OUTPUT): os.remove(DOWNLOAD_OUTPUT)
        if os.path.exists(SHORTS_OUTPUT): os.remove(SHORTS_OUTPUT)
        print("[+] Automation task finished successfully.")
        
    except Exception as e:
        print(f"[-] Error occurred: {str(e)}")
