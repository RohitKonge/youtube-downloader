from flask import Flask, render_template, request, send_file, after_this_request, jsonify, Response, stream_with_context
import yt_dlp
import os
import uuid
import re
import threading
import time
import shutil
import logging
import traceback
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('youtube-downloader')

app = Flask(__name__)

# Create a downloads directory if it doesn't exist
DOWNLOAD_DIR = os.path.join(os.getcwd(), 'downloads')
if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)
    logger.info(f"Created downloads directory at {DOWNLOAD_DIR}")

# Global dictionary to track download progress
download_progress = {}

# Cleanup old downloads on startup


def cleanup_old_downloads():
    if os.path.exists(DOWNLOAD_DIR):
        count = 0
        for file in os.listdir(DOWNLOAD_DIR):
            try:
                file_path = os.path.join(DOWNLOAD_DIR, file)
                if os.path.isfile(file_path):
                    os.remove(file_path)
                    count += 1
            except Exception as e:
                logger.error(f"Error cleaning up old download: {e}")
        logger.info(f"Cleaned up {count} old downloads at startup")


# Run cleanup at startup
cleanup_old_downloads()


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

    logger.info(f"Download requested for URL: {url} at {resolution}p")

    download_id = str(uuid.uuid4())
    temp_filename = os.path.join(DOWNLOAD_DIR, f"{download_id}.mp4")

    # Get video title for final filename
    try:
        with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            video_title = info.get('title', 'video')
            video_duration = info.get('duration', 0)
            logger.info(
                f"Video info: Title={video_title}, Duration={video_duration}s")
    except Exception as e:
        logger.error(f"Error getting video info: {e}")
        video_title = 'video'
        video_duration = 0

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
        'final_filename': final_filename,
        'duration': video_duration,
        'start_time': datetime.now().timestamp(),
        'url': url
    }

    # Define progress hook
    def progress_hook(d):
        if d['status'] == 'downloading':
            # Calculate percentage
            downloaded_bytes = d.get('downloaded_bytes', 0)

            if 'total_bytes' in d and d['total_bytes'] > 0:
                percent = downloaded_bytes / d['total_bytes'] * 100
                total_bytes = d['total_bytes']
            elif 'total_bytes_estimate' in d and d['total_bytes_estimate'] > 0:
                percent = downloaded_bytes / d['total_bytes_estimate'] * 100
                total_bytes = d['total_bytes_estimate']
            else:
                percent = 0
                total_bytes = 0

            # Log progress every 10%
            current_percent = round(percent, 1)
            previous_percent = download_progress[download_id].get(
                'progress', 0)
            if int(current_percent / 10) > int(previous_percent / 10):
                logger.info(
                    f"Download {download_id}: {current_percent}% ({downloaded_bytes/(1024*1024):.2f}MB / {total_bytes/(1024*1024):.2f}MB)")

            download_progress[download_id]['progress'] = current_percent
            download_progress[download_id]['status'] = 'downloading'
            download_progress[download_id]['downloaded_bytes'] = downloaded_bytes

            # Store file size information
            if 'total_bytes' in d:
                download_progress[download_id]['total_bytes'] = d['total_bytes']
            elif 'total_bytes_estimate' in d:
                download_progress[download_id]['total_bytes'] = d['total_bytes_estimate']

        elif d['status'] == 'finished':
            download_progress[download_id]['progress'] = 100
            # Now processing (merging audio/video)
            download_progress[download_id]['status'] = 'processing'
            logger.info(
                f"Download {download_id}: Download finished, now processing")

    # Set format based on selected resolution
    format_string = f'bestvideo[height<={resolution}]+bestaudio/best[height<={resolution}]'

    ydl_opts = {
        'format': format_string,
        'merge_output_format': 'mp4',
        'outtmpl': temp_filename,
        'quiet': True,
        'progress_hooks': [progress_hook],
        # Add fragment retries and limit fragment size for better handling
        'fragment_retries': 10,
        'retries': 10,
        'file_access_retries': 5,
        'retry_sleep_functions': {'fragment': lambda n: 1 + n/3},
        'socket_timeout': 60,  # Increase socket timeout
    }

    # Start download in a separate thread
    threading.Thread(target=download_thread, args=(
        url, ydl_opts, download_id), daemon=True).start()

    # Return the download ID so the frontend can poll for progress
    logger.info(f"Started download with ID: {download_id}")
    return jsonify({
        'success': True,
        'download_id': download_id
    })


def download_thread(url, ydl_opts, download_id):
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # Check if file exists and has non-zero size
        file_path = download_progress[download_id]['file_path']
        if not os.path.exists(file_path):
            raise Exception(
                f"Download completed but file not found: {file_path}")

        file_size = os.path.getsize(file_path)
        if file_size == 0:
            raise Exception("Download completed but file is empty (0 bytes)")

        # Download and processing completed
        download_progress[download_id]['status'] = 'completed'
        download_progress[download_id]['file_size'] = file_size

        elapsed_time = datetime.now().timestamp(
        ) - download_progress[download_id]['start_time']
        logger.info(
            f"Download {download_id} completed successfully: {file_size/(1024*1024):.2f}MB in {elapsed_time:.2f}s")

        # Auto-cleanup after 30 minutes
        def cleanup_download():
            try:
                time.sleep(1800)  # 30 minutes
                if download_id in download_progress:
                    file_path = download_progress[download_id]['file_path']
                    if os.path.exists(file_path):
                        os.remove(file_path)
                        logger.info(f"Cleaned up download file: {file_path}")
                    del download_progress[download_id]
                    logger.info(
                        f"Removed download {download_id} from tracking")
            except Exception as e:
                logger.error(f"Error cleaning up download {download_id}: {e}")

        threading.Thread(target=cleanup_download, daemon=True).start()

    except Exception as e:
        download_progress[download_id]['status'] = 'error'
        download_progress[download_id]['error_message'] = str(e)
        logger.error(f"Download error for {download_id}: {e}")
        logger.error(traceback.format_exc())


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
        logger.warning(f"Download not found: {download_id}")
        return "Download not found", 404

    if download_progress[download_id]['status'] != 'completed':
        current_status = download_progress[download_id]['status']
        logger.warning(
            f"Download not complete: {download_id}, status: {current_status}")
        return "Download not complete", 400

    file_path = download_progress[download_id]['file_path']
    filename = download_progress[download_id]['final_filename']

    if not os.path.exists(file_path):
        logger.error(f"File not found: {file_path}")
        return "File not found", 404

    # Log the start of download
    file_size = os.path.getsize(file_path)
    logger.info(
        f"Starting to serve file: {filename}, size: {file_size/(1024*1024):.2f}MB")

    # Stream the file in chunks instead of loading it all into memory
    def generate():
        bytes_sent = 0
        chunk_count = 0
        start_time = time.time()

        try:
            with open(file_path, 'rb') as f:
                chunk_size = 1024 * 1024  # 1MB chunks for better performance
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    bytes_sent += len(chunk)
                    chunk_count += 1

                    # Log progress for large files
                    if chunk_count % 10 == 0:  # Log every 10MB
                        percent = (bytes_sent / file_size) * 100
                        elapsed = time.time() - start_time
                        speed = bytes_sent / \
                            (1024 * 1024 * elapsed) if elapsed > 0 else 0
                        logger.info(
                            f"Serving {download_id}: {percent:.1f}% ({bytes_sent/(1024*1024):.2f}MB / {file_size/(1024*1024):.2f}MB), {speed:.2f} MB/s")

                    yield chunk

            # Log completion
            total_time = time.time() - start_time
            speed = file_size / \
                (1024 * 1024 * total_time) if total_time > 0 else 0
            logger.info(
                f"Completed serving file {download_id}: {file_size/(1024*1024):.2f}MB in {total_time:.2f}s ({speed:.2f} MB/s)")

        except Exception as e:
            logger.error(f"Error streaming file {download_id}: {e}")
            logger.error(traceback.format_exc())

    response = Response(stream_with_context(generate()),
                        mimetype='video/mp4')
    response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    response.headers['Content-Length'] = str(file_size)

    return response


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
