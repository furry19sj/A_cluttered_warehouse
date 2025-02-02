import re
import os
import uuid
import requests
import socket
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote, quote
from bs4 import BeautifulSoup
import unicodedata
from threading import Thread, Event, Lock
import subprocess
import time
from pathlib import Path
from datetime import datetime

# 配置参数
PORT = 6969
CACHE_ROOT = os.path.join(os.path.expanduser('~'), 'Desktop', '163-cache')
MP3_DIR = os.path.join(CACHE_ROOT, 'mp3')
LOG_DIR = os.path.join(CACHE_ROOT, 'log')
CLEANUP_INTERVAL = 3600
FILE_EXPIRE_TIME = 1800

# 默认选择器配置
DEFAULT_SELECTOR = ('em', {'class': 'f-ff2'})

# 初始化目录结构
os.makedirs(MP3_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

class DownloadManager:
    def __init__(self):
        self.lock = Lock()
        self.download_tokens = {}
        self.selector = DEFAULT_SELECTOR
        self.last_selector_update = 0

    def update_selector(self):
        """每小时更新一次选择器"""
        if time.time() - self.last_selector_update < 3600:
            return
        try:
            response = requests.get(
                "https://furry19.top/tool/music-163-download-html.txt",
                timeout=5
            )
            if response.status_code == 200:
                config = response.text.strip()
                match = re.search(r'<([a-zA-Z]+)\s+class="([^"]+)"', config)
                if match:
                    self.selector = (match.group(1), {'class': match.group(2)})
                    self.last_selector_update = time.time()
        except Exception as e:
            print(f"选择器更新失败: {str(e)}")

    def generate_download_token(self, filename):
        token = str(uuid.uuid4())
        with self.lock:
            self.download_tokens[token] = {
                'filename': filename,
                'expire': time.time() + 300
            }
        return token

    def validate_token(self, token):
        with self.lock:
            data = self.download_tokens.pop(token, None)
            if data and time.time() < data['expire']:
                return data['filename']
        return None

download_manager = DownloadManager()

class FileCleaner(Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.stop_event = Event()

    def run(self):
        while not self.stop_event.is_set():
            self.clean_files()
            time.sleep(CLEANUP_INTERVAL)

    def clean_files(self):
        now = time.time()
        for f in Path(MP3_DIR).glob('*.mp3'):
            if now - f.stat().st_mtime > FILE_EXPIRE_TIME:
                try:
                    f.unlink()
                    self.log(f"Cleaned: {f.name}")
                except Exception as e:
                    self.log(f"Clean failed: {str(e)}")

    def log(self, message):
        log_path = Path(LOG_DIR) / 'clean.log'
        with log_path.open('a', encoding='utf-8') as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")

class MusicRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if self.path == '/':
                self.serve_html()
            elif self.path.startswith('/download/'):
                self.handle_download()
            else:
                self.send_error(404)
        except Exception as e:
            self.log_error(str(e))
            self.send_error(500)

    def do_POST(self):
        try:
            if self.path == '/submit':
                content_length = int(self.headers['Content-Length'])
                post_data = self.rfile.read(content_length).decode('utf-8')
                self.handle_submit(post_data)
            else:
                self.send_error(404)
        except Exception as e:
            self.log_error(str(e))
            self.send_error(500, explain="Internal Server Error")

    def handle_submit(self, post_data):
        share_link = unquote(parse_qs(post_data)['link'][0])
        song_id = extract_song_id(share_link)
        download_manager.update_selector()
        song_name = get_song_name(song_id, download_manager.selector)
        filename = generate_filename(song_id, song_name)
        
        filepath = download_music(song_id, filename)
        token = download_manager.generate_download_token(filename)
        log_download(song_id, filename)
        
        self.send_json_response({
            'download_url': f'/download/{token}',
            'filename': filename
        })

    def serve_html(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(b'''
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <title>Music Download Service</title>
            <style>
                body { max-width: 800px; margin: 20px auto; padding: 20px; }
                .container { text-align: center; }
                input { width: 300px; padding: 8px; }
                button { padding: 8px 20px; margin-left: 10px; }
                #result { margin-top: 20px; color: #666; }
            </style>
        </head>
        <body>
            <div class="container">
                <h1>NetEase Music Download</h1>
                <input type="text" id="link" placeholder="Paste share link or song ID">
                <button onclick="handleSubmit()">Download</button>
                <div id="result"></div>
            </div>
            <script>
                let isProcessing = false;
                function handleSubmit() {
                    if (isProcessing) return;
                    isProcessing = true;
                    
                    const link = document.getElementById('link').value;
                    const resultDiv = document.getElementById('result');
                    resultDiv.innerHTML = 'Processing...';
                    
                    fetch('/submit', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                        body: 'link=' + encodeURIComponent(link)
                    })
                    .then(response => {
                        if (!response.ok) throw new Error('Server Error');
                        return response.json();
                    })
                    .then(data => {
                        const a = document.createElement('a');
                        a.href = data.download_url;
                        a.download = data.filename;
                        document.body.appendChild(a);
                        a.click();
                        document.body.removeChild(a);
                        resultDiv.innerHTML = `Download started: ${data.filename}`;
                    })
                    .catch(error => {
                        resultDiv.innerHTML = 'Error: ' + error.message;
                    })
                    .finally(() => {
                        isProcessing = false;
                    });
                }
            </script>
        </body>
        </html>
        ''')

    def handle_download(self):
        token = self.path.split('/')[-1]
        filename = download_manager.validate_token(token)
        
        if not filename:
            self.send_error(410, "Download link expired")
            return

        filepath = Path(MP3_DIR) / filename
        if not filepath.exists():
            self.send_error(404)
            return

        quoted_filename = quote(filename.encode('utf-8'))
        self.send_response(200)
        self.send_header('Content-Type', 'audio/mpeg')
        self.send_header('Content-Disposition', 
                       f'attachment; filename*=UTF-8''{quoted_filename}')
        self.send_header('Content-Length', filepath.stat().st_size)
        self.end_headers()
        
        try:
            with open(filepath, 'rb') as f:
                self.wfile.write(f.read())
        except ConnectionAbortedError:
            self.log_error(f"Download aborted: {filename}")

    def send_json_response(self, data):
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_error(self, message):
        log_path = Path(LOG_DIR) / 'error.log'
        with log_path.open('a', encoding='utf-8') as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")

def sanitize_filename(name):
    """允许日文字符的安全文件名处理"""
    allowed_chars = r'[^\w\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF\s\(\)&.-]'
    cleaned = unicodedata.normalize('NFKC', name)
    cleaned = re.sub(allowed_chars, '', cleaned)
    cleaned = cleaned.replace('/', '-').strip()
    return cleaned[:150]

def generate_filename(song_id, song_name):
    """生成最终文件名"""
    if song_name:
        return f"{sanitize_filename(song_name)}.mp3"
    return f"{song_id}.mp3"

def extract_song_id(input_str):
    if input_str.strip().isdigit():
        return input_str.strip()
    
    match = re.search(r'https?://[^\s]+', input_str)
    if not match:
        raise ValueError("Invalid share link")
    
    try:
        response = requests.head(match.group(0), allow_redirects=True, timeout=10)
        parsed_url = urlparse(response.url)
        return parse_qs(parsed_url.query)['id'][0]
    except Exception as e:
        raise ValueError(f"Link parse failed: {str(e)}")

def get_song_name(song_id, selector):
    try:
        url = f"https://music.163.com/song?id={song_id}"
        response = requests.get(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }, timeout=15)
        soup = BeautifulSoup(response.text, 'html.parser')
        name_tag = soup.find(*selector)
        return name_tag.text.strip() if name_tag else None
    except Exception as e:
        print(f"获取歌曲名失败: {str(e)}")
        return None

def download_music(song_id, filename):
    download_url = f"http://music.163.com/song/media/outer/url?id={song_id}.mp3"
    try:
        response = requests.get(download_url, stream=True, timeout=30)
        if 'audio/mpeg' not in response.headers.get('Content-Type', ''):
            raise ValueError("Invalid audio response")
        
        filepath = Path(MP3_DIR) / filename
        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        
        if os.name == 'nt':
            subprocess.run(
                ['icacls', str(filepath), '/inheritance:r', '/grant:r', 'Everyone:R'],
                check=True
            )
        return filepath
    except Exception as e:
        if 'filepath' in locals():
            Path(filepath).unlink(missing_ok=True)
        raise

def log_download(song_id, filename):
    log_path = Path(LOG_DIR) / 'download.log'
    with log_path.open('a', encoding='utf-8') as f:
        f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ID:{song_id} File:{filename}\n")

def local_download_loop():
    while True:
        try:
            user_input = input("\n请输入歌曲ID或分享链接（输入q退出）: ").strip()
            if user_input.lower() == 'q':
                break
            
            song_id = extract_song_id(user_input)
            download_manager.update_selector()
            song_name = get_song_name(song_id, download_manager.selector)
            filename = generate_filename(song_id, song_name)
            
            print(f"开始下载: {filename}")
            filepath = download_music(song_id, filename)
            print(f"下载完成: {filepath}")
            
        except Exception as e:
            print(f"错误: {str(e)}")

def open_firewall_port():
    try:
        subprocess.run([
            'netsh', 'advfirewall', 'firewall', 'add', 'rule',
            f'name=MusicDL_Port_{PORT}', 'dir=in', 'action=allow',
            'protocol=TCP', f'localport={PORT}'
        ], check=True, shell=True)
        print(f"已开通防火墙端口 {PORT}")
    except subprocess.CalledProcessError:
        print(f"需要手动开放端口 {PORT}")

def main():
    for path in [CACHE_ROOT, MP3_DIR, LOG_DIR]:
        Path(path).mkdir(parents=True, exist_ok=True)
    
    FileCleaner().start()
    open_firewall_port()

    server = ThreadingHTTPServer(('0.0.0.0', PORT), MusicRequestHandler)
    print(f"HTTP服务已启动: http://{socket.gethostbyname(socket.gethostname())}:{PORT}")

    local_thread = Thread(target=local_download_loop, daemon=True)
    local_thread.start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
        print("\n服务已关闭")

if __name__ == '__main__':
    main()