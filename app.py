from flask import Flask, render_template, request, send_file, after_this_request, jsonify
import yt_dlp
import os
import uuid
import re
import threading
import time

app = Flask(__name__)

# Global dictionary to track download progress
download_progress = {}


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/video-info')
def video_info():
    url = request.args.get('url', '')

    if not url:
        return jsonify({'success': False, 'error': 'No URL provided'})

    # Extract video ID from URL
    video_id = extract_video_id(url)
    if not video_id:
        return jsonify({'success': False, 'error': 'Invalid YouTube URL'})

    try:
        with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
            info = ydl.extract_info(url, download=False)

        return jsonify({
            'success': True,
            'title': info.get('title', 'Unknown Title'),
            'thumbnail': info.get('thumbnail', ''),
            'duration': info.get('duration', 0)
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/download', methods=['POST'])
def download_video():
    url = request.form['url']
    # Default to 1080p if not specified
    resolution = request.form.get('resolution', '1080')

    download_id = str(uuid.uuid4())
    temp_filename = f"{download_id}.mp4"

    # Get video title for final filename
    try:
        with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            video_title = info.get('title', 'video')
    except:
        video_title = 'video'

    # Sanitize the title to create a valid filename
    video_title = sanitize_filename(video_title)

    # Ensure the filename isn't too long
    if len(video_title) > 100:
        video_title = video_title[:100]

    # Add resolution to the filename
    final_filename = f"{video_title}_{resolution}p.mp4"

    # Set download progress to 0
    download_progress[download_id] = {
        'progress': 0,
        'status': 'starting',
        'file_path': temp_filename,
        'title': video_title,
        'final_filename': final_filename
    }

    # Define progress hook
    def progress_hook(d):
        if d['status'] == 'downloading':
            # Calculate percentage
            if 'total_bytes' in d and d['total_bytes'] > 0:
                percent = d['downloaded_bytes'] / d['total_bytes'] * 100
            elif 'total_bytes_estimate' in d and d['total_bytes_estimate'] > 0:
                percent = d['downloaded_bytes'] / \
                    d['total_bytes_estimate'] * 100
            else:
                percent = 0

            download_progress[download_id]['progress'] = round(percent, 1)
            download_progress[download_id]['status'] = 'downloading'

        elif d['status'] == 'finished':
            download_progress[download_id]['progress'] = 100
            # Now processing (merging audio/video)
            download_progress[download_id]['status'] = 'processing'

    # Set format based on selected resolution
    format_string = f'bestvideo[height<={resolution}]+bestaudio/best[height<={resolution}]'

    ydl_opts = {
        'format': format_string,
        'merge_output_format': 'mp4',
        'outtmpl': temp_filename,
        'quiet': True,
        'progress_hooks': [progress_hook],
        'retries': 100,
        'fragment_retries': 100,
        'concurrent_fragment_downloads': 5,
    }

    # Start download in a separate thread
    threading.Thread(target=download_thread, args=(
        url, ydl_opts, download_id)).start()

    # Return the download ID so the frontend can poll for progress
    return jsonify({
        'success': True,
        'download_id': download_id
    })


def download_thread(url, ydl_opts, download_id):
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # Download and processing completed
        download_progress[download_id]['status'] = 'completed'

        # Auto-cleanup after 10 minutes
        def cleanup_download():
            time.sleep(600)  # 10 minutes
            if download_id in download_progress:
                try:
                    file_path = download_progress[download_id]['file_path']
                    if os.path.exists(file_path):
                        os.remove(file_path)
                    del download_progress[download_id]
                except:
                    pass

        threading.Thread(target=cleanup_download).start()

    except Exception as e:
        download_progress[download_id]['status'] = 'error'
        download_progress[download_id]['error_message'] = str(e)


@app.route('/download-progress/<download_id>')
def get_download_progress(download_id):
    if download_id not in download_progress:
        return jsonify({'success': False, 'error': 'Download not found'})

    return jsonify({
        'success': True,
        'progress': download_progress[download_id]['progress'],
        'status': download_progress[download_id]['status']
    })


@app.route('/get-file/<download_id>')
def get_file(download_id):
    if download_id not in download_progress:
        return "Download not found", 404

    if download_progress[download_id]['status'] != 'completed':
        return "Download not complete", 400

    file_path = download_progress[download_id]['file_path']
    filename = download_progress[download_id]['final_filename']

    if not os.path.exists(file_path):
        return "File not found", 404

    @after_this_request
    def cleanup(response):
        # We'll keep the file for the auto-cleanup thread to handle
        return response

    return send_file(file_path, as_attachment=True, download_name=filename)


def extract_video_id(url):
    """Extract the video ID from a YouTube URL."""
    # YouTube URL patterns
    patterns = [
        r'(?:v=|\/)([0-9A-Za-z_-]{11}).*',  # Standard YouTube URLs
        r'(?:embed\/)([0-9A-Za-z_-]{11})',  # Embed URLs
        r'(?:youtu\.be\/)([0-9A-Za-z_-]{11})'  # Shortened youtu.be URLs
    ]

    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)

    return None


def sanitize_filename(filename):
    """Remove characters that are invalid in filenames."""
    # Remove characters that aren't alphanumeric, underscore, dash, space, or period
    sanitized = re.sub(r'[^\w\-\. ]', '_', filename)
    # Replace multiple spaces with a single space
    sanitized = re.sub(r'\s+', ' ', sanitized)
    # Remove leading/trailing spaces
    sanitized = sanitized.strip()
    return sanitized


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
