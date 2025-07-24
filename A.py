from flask import Flask, render_template, request, jsonify, send_file, session, redirect, url_for, flash, after_this_request
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
import yt_dlp
import os
import uuid
from datetime import datetime
import threading
import time
from werkzeug.security import generate_password_hash, check_password_hash
import tempfile # Import tempfile for temporary directory creation
import shutil # Import shutil for removing temporary directories

app = Flask(__name__)
# IMPORTANT: Change this to a strong, unique secret key!
app.config['SECRET_KEY'] = 'your-secret-key-here-please-change-this-for-security'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///youtube_downloader.db'
app.config['UPLOAD_FOLDER'] = 'downloads' # This folder will now store permanent downloads

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Create downloads directory if it doesn't exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# --- Moved @app.after_request to global scope (Fix for "after_request" error) ---
@app.after_request
def add_header(response):
    # This ensures no caching issues with dynamic content, important for status updates
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response
# --- End of moved section ---

# Database Models
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(128))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    downloads = db.relationship('Download', backref='user', lazy=True)

class Download(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    url = db.Column(db.String(500), nullable=False)
    download_type = db.Column(db.String(10), nullable=False)  # 'video' or 'audio'
    status = db.Column(db.String(20), default='pending')  # pending, downloading, completed, failed
    filename = db.Column(db.String(200)) # This will now store the filename in UPLOAD_FOLDER
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Routes
@app.route('/')
def index():
    if current_user.is_authenticated:
        recent_downloads = Download.query.filter_by(user_id=current_user.id).order_by(Download.created_at.desc()).limit(5).all()
        return render_template('dashboard.html', downloads=recent_downloads)
    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        data = request.get_json()
        username = data.get('username')
        email = data.get('email')
        password = data.get('password')
        
        if User.query.filter_by(username=username).first():
            return jsonify({'success': False, 'message': 'Username already exists'})
        
        if User.query.filter_by(email=email).first():
            return jsonify({'success': False, 'message': 'Email already exists'})
        
        password_hash = generate_password_hash(password)
        user = User(username=username, email=email, password_hash=password_hash)
        db.session.add(user)
        db.session.commit()
        
        login_user(user)
        return jsonify({'success': True, 'message': 'Registration successful'})
    
    return render_template('auth.html', mode='register')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        data = request.get_json()
        username = data.get('username')
        password = data.get('password')
        
        user = User.query.filter_by(username=username).first()
        
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            return jsonify({'success': True, 'message': 'Login successful'})
        
        return jsonify({'success': False, 'message': 'Invalid credentials'})
    
    return render_template('auth.html', mode='login')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/dashboard')
@login_required
def dashboard():
    downloads = Download.query.filter_by(user_id=current_user.id).order_by(Download.created_at.desc()).all()
    return render_template('dashboard.html', downloads=downloads)

# MODIFIED /download route for direct synchronous download
@app.route('/download', methods=['POST'])
@login_required
def download():
    url = request.form.get('url') # Get from form data, not JSON
    download_type = request.form.get('type') # Get from form data, not JSON
    
    if not url:
        flash('URL is required', 'error')
        return redirect(url_for('dashboard')) # Redirect back to dashboard

    temp_dir = None
    download_record = None
    final_download_path = None # To store the path to the file in the permanent UPLOAD_FOLDER
    try:
        # Create a temporary directory for this download
        # It's good practice to create temp directories within a known location
        temp_dir = tempfile.mkdtemp(dir=app.config['UPLOAD_FOLDER']) 
        
        # Get video info first (without downloading)
        ydl_opts_info = {'quiet': True, 'skip_download': True}
        with yt_dlp.YoutubeDL(ydl_opts_info) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get('title', 'Unknown Title')
            
        # Generate a unique filename for the downloaded file in the temp directory
        unique_id = str(uuid.uuid4())
        
        # Determine output template based on type
        if download_type == 'audio':
            output_template = os.path.join(temp_dir, f"{unique_id}.%(ext)s")
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': output_template,
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
                'noplaylist': True,
            }
            download_extension = 'mp3'
        else: # video
            output_template = os.path.join(temp_dir, f"{unique_id}.%(ext)s")
            ydl_opts = {
                'format': 'bestvideo[height<=720]+bestaudio/best[height<=720]/best', # Prioritize 720p, merge video+audio
                'outtmpl': output_template,
                'noplaylist': True,
                'merge_output_format': 'mp4', # Ensure output is mp4 after merging
            }
            download_extension = 'mp4'

        # Create download record (status will be updated later)
        download_record = Download(
            user_id=current_user.id,
            title=title,
            url=url,
            download_type=download_type,
            status='downloading' # Set to downloading while processing
        )
        db.session.add(download_record)
        db.session.commit() # Commit to get an ID for the record

        # Download the file synchronously
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            download_info = ydl.extract_info(url, download=True)
            
            # Find the actual downloaded file path in the temp_dir
            actual_filepath_in_temp = None
            for fname in os.listdir(temp_dir):
                if fname.startswith(unique_id): # yt-dlp might add extra info to the filename (e.g., .f137.mp4)
                    actual_filepath_in_temp = os.path.join(temp_dir, fname)
                    break
            
            if not actual_filepath_in_temp or not os.path.exists(actual_filepath_in_temp):
                raise Exception("Downloaded file not found or path is incorrect in temporary directory.")
            
            # Construct a user-friendly download name for the client
            safe_title = "".join([c for c in title if c.isalnum() or c in (' ', '.', '_', '-')]).strip()
            if len(safe_title) > 100:
                safe_title = safe_title[:100]
            
            # Use a unique filename for the permanently stored file to avoid conflicts
            permanent_filename = f"{unique_id}_{safe_title}.{download_extension}"
            final_download_path = os.path.join(app.config['UPLOAD_FOLDER'], permanent_filename)

            # Move the downloaded file from temp_dir to the permanent UPLOAD_FOLDER
            shutil.move(actual_filepath_in_temp, final_download_path)
            print(f"Moved file from {actual_filepath_in_temp} to {final_download_path}")

            # Update download record status to completed and store permanent filename
            download_record.status = 'completed'
            download_record.filename = permanent_filename # Store the name of the file in UPLOAD_FOLDER
            db.session.commit()

            # Clean up the temporary directory immediately after moving the file
            if temp_dir and os.path.exists(temp_dir):
                try:
                    shutil.rmtree(temp_dir)
                    print(f"Cleaned up temporary directory: {temp_dir}")
                except Exception as e:
                    print(f"Error cleaning up temporary directory {temp_dir}: {e}")
            
            # Send the file to the client from its permanent location
            flash('Download completed successfully!', 'success')
            return send_file(final_download_path, as_attachment=True, download_name=f"{safe_title}.{download_extension}")

    except Exception as e:
        print(f"[ERROR] Download failed: {str(e)}")
        if download_record:
            download_record.status = 'failed'
            db.session.commit()
        # Clean up temp directory immediately if an error occurs
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
                print(f"Cleaned up temporary directory on error: {temp_dir}")
            except Exception as cleanup_e:
                print(f"Error during error cleanup of {temp_dir}: {cleanup_e}")
        
        flash(f"Download failed: {str(e)}", 'error')
        return redirect(url_for('dashboard')) # Redirect back with error

# The /download_file/<filename> route now serves files from the permanent UPLOAD_FOLDER
@app.route('/download_file/<filename>')
@login_required
def download_file(filename):
    # Verify user owns this download
    download = Download.query.filter_by(filename=filename, user_id=current_user.id).first()
    if not download or download.status != 'completed':
        flash("File not found or not ready.", 'error')
        return redirect(url_for('dashboard'))
    
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    if os.path.exists(file_path):
        # Use the title from the database for the download name, append correct extension
        download_name = download.title + ('.mp3' if download.download_type == 'audio' else '.mp4')
        # Sanitize download_name for browser and limit length
        safe_download_name = "".join([c for c in download_name if c.isalnum() or c in (' ', '.', '_', '-')]).strip()
        if len(safe_download_name) > 100:
            safe_download_name = safe_download_name[:100] + ('.mp3' if download.download_type == 'audio' else '.mp4') # Re-add extension if truncated
        
        return send_file(file_path, as_attachment=True, download_name=safe_download_name)
    flash("File not found on server.", 'error')
    return redirect(url_for('dashboard'))


@app.route('/check_status')
@login_required
def check_status():
    downloads = Download.query.filter_by(user_id=current_user.id).order_by(Download.created_at.desc()).limit(10).all()
    download_list = []
    for d in downloads:
        download_list.append({
            'id': d.id,
            'title': d.title,
            'url': d.url, # Include URL for debugging/reference
            'type': d.download_type,
            'status': d.status,
            'filename': d.filename,
            'created_at': d.created_at.strftime('%Y-%m-%d %H:%M:%S')
        })
    return jsonify({'downloads': download_list})

# Template files (leave as is, or run create_templates() once)
@app.route('/create_templates')
def create_templates():
    templates_dir = 'templates'
    os.makedirs(templates_dir, exist_ok=True)
    
    # Base template
    base_template = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>YouTube Downloader</title>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/tailwindcss/2.2.19/tailwind.min.css" rel="stylesheet">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
    <style>
        .theme-dark {
            --bg-primary: #1a1a1a;
            --bg-secondary: #2d2d2d;
            --text-primary: #ffffff;
            --text-secondary: #a3a3a3;
            --border-color: #404040;
        }
        .theme-light {
            --bg-primary: #ffffff;
            --bg-secondary: #f8fafc;
            --text-primary: #1a1a1a;
            --text-secondary: #64748b;
            --border-color: #e2e8f0;
        }
        body {
            background-color: var(--bg-primary);
            color: var(--text-primary);
            transition: all 0.3s ease;
        }
        .card {
            background-color: var(--bg-secondary);
            border-color: var(--border-color);
            transition: all 0.3s ease;
        }
    </style>
</head>
<body class="theme-light" id="body">
    <nav class="card border-b p-4">
        <div class="container mx-auto flex justify-between items-center">
            <h1 class="text-2xl font-bold">
                <i class="fas fa-download mr-2"></i>
                YouTube Downloader
            </h1>
            <div class="flex items-center space-x-4">
                <button onclick="toggleTheme()" class="p-2 rounded-lg card border hover:opacity-80 transition-opacity">
                    <i class="fas fa-moon" id="theme-icon"></i>
                </button>
                {% if current_user.is_authenticated %}
                    <span class="text-sm opacity-75">Welcome, {{ current_user.username }}</span>
                    <a href="{{ url_for('logout') }}" class="bg-red-600 text-white px-4 py-2 rounded-lg hover:bg-red-700 transition-colors">
                        Logout
                    </a>
                {% else %}
                    <a href="{{ url_for('login') }}" class="bg-blue-600 text-white px-4 py-2 rounded-lg hover:bg-blue-700 transition-colors">
                        Login
                    </a>
                {% endif %}
            </div>
        </div>
    </nav>
    
    <main class="container mx-auto px-4 py-8">
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                <div class="mb-4">
                    {% for category, message in messages %}
                        <div class="p-3 rounded-lg {% if category == 'error' %}bg-red-500{% else %}bg-green-500{% endif %} text-white mb-2">
                            {{ message }}
                        </div>
                    {% endfor %}
                </div>
            {% endif %}
        {% endwith %}
        {% block content %}{% endblock %}
    </main>
    
    <script>
        function toggleTheme() {
            const body = document.getElementById('body');
            const themeIcon = document.getElementById('theme-icon');
            
            if (body.classList.contains('theme-light')) {
                body.classList.remove('theme-light');
                body.classList.add('theme-dark');
                themeIcon.classList.remove('fa-moon');
                themeIcon.classList.add('fa-sun');
                localStorage.setItem('theme', 'dark');
            } else {
                body.classList.remove('theme-dark');
                body.classList.add('theme-light');
                themeIcon.classList.remove('fa-sun');
                themeIcon.classList.add('fa-moon');
                localStorage.setItem('theme', 'light');
            }
        }
        
        // Load saved theme
        document.addEventListener('DOMContentLoaded', function() {
            const savedTheme = localStorage.getItem('theme') || 'light';
            const body = document.getElementById('body');
            const themeIcon = document.getElementById('theme-icon');
            
            if (savedTheme === 'dark') {
                body.classList.remove('theme-light');
                body.classList.add('theme-dark');
                themeIcon.classList.remove('fa-moon');
                themeIcon.classList.add('fa-sun');
            }
        });
    </script>
</body>
</html>'''
    
    with open(os.path.join(templates_dir, 'base.html'), 'w') as f:
        f.write(base_template)
    
    # Index template
    index_template = '''{% extends "base.html" %}

{% block content %}
<div class="max-w-4xl mx-auto">
    <div class="text-center mb-12">
        <h2 class="text-4xl font-bold mb-4">Download YouTube Videos & Audio</h2>
        <p class="text-xl opacity-75">Fast, secure, and easy to use YouTube downloader</p>
    </div>
    
    <div class="grid md:grid-cols-2 gap-8">
        <div class="card border rounded-xl p-8">
            <div class="text-center mb-6">
                <i class="fas fa-video text-4xl text-blue-600 mb-4"></i>
                <h3 class="text-2xl font-bold mb-2">Video Downloads</h3>
                <p class="opacity-75">Download high-quality videos up to 720p resolution</p>
            </div>
        </div>
        
        <div class="card border rounded-xl p-8">
            <div class="text-center mb-6">
                <i class="fas fa-music text-4xl text-green-600 mb-4"></i>
                <h3 class="text-2xl font-bold mb-2">Audio Downloads</h3>
                <p class="opacity-75">Extract audio in MP3 format with 192kbps quality</p>
            </div>
        </div>
    </div>
    
    <div class="card border rounded-xl p-8 mt-8 text-center">
        <h3 class="text-2xl font-bold mb-4">Get Started</h3>
        <p class="opacity-75 mb-6">Create an account to start downloading your favorite YouTube content</p>
        <div class="space-x-4">
            <a href="{{ url_for('register') }}" class="bg-blue-600 text-white px-6 py-3 rounded-lg hover:bg-blue-700 transition-colors inline-block">
                Sign Up Free
            </a>
            <a href="{{ url_for('login') }}" class="card border px-6 py-3 rounded-lg hover:opacity-80 transition-opacity inline-block">
                Login
            </a>
        </div>
    </div>
</div>
{% endblock %}'''
    
    with open(os.path.join(templates_dir, 'index.html'), 'w') as f:
        f.write(index_template)
    
    # Auth template
    auth_template = '''{% extends "base.html" %}

{% block content %}
<div class="max-w-md mx-auto">
    <div class="card border rounded-xl p-8">
        <div class="text-center mb-8">
            <h2 class="text-3xl font-bold mb-2">
                {% if mode == 'register' %}Create Account{% else %}Welcome Back{% endif %}
            </h2>
            <p class="opacity-75">
                {% if mode == 'register' %}Join us to start downloading{% else %}Sign in to your account{% endif %}
            </p>
        </div>
        
        <form id="authForm">
            <div class="space-y-4">
                <div>
                    <label class="block text-sm font-medium mb-2">Username</label>
                    <input type="text" id="username" name="username" required 
                            class="w-full px-4 py-3 card border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500">
                </div>
                
                {% if mode == 'register' %}
                <div>
                    <label class="block text-sm font-medium mb-2">Email</label>
                    <input type="email" id="email" name="email" required 
                            class="w-full px-4 py-3 card border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500">
                </div>
                {% endif %}
                
                <div>
                    <label class="block text-sm font-medium mb-2">Password</label>
                    <input type="password" id="password" name="password" required 
                            class="w-full px-4 py-3 card border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500">
                </div>
                
                <button type="submit" class="w-full bg-blue-600 text-white py-3 rounded-lg hover:bg-blue-700 transition-colors">
                    {% if mode == 'register' %}Create Account{% else %}Sign In{% endif %}
                </button>
            </div>
        </form>
        
        <div class="text-center mt-6">
            {% if mode == 'register' %}
                <p class="opacity-75">Already have an account? 
                    <a href="{{ url_for('login') }}" class="text-blue-600 hover:underline">Sign in</a>
                </p>
            {% else %}
                <p class="opacity-75">Don't have an account? 
                    <a href="{{ url_for('register') }}" class="text-blue-600 hover:underline">Sign up</a>
                </p>
            {% endif %}
        </div>
    </div>
</div>

<script>
document.getElementById('authForm').addEventListener('submit', async function(e) {
    e.preventDefault();
    
    const formData = new FormData(this);
    const data = Object.fromEntries(formData);
    
    try {
        const response = await fetch('{{ request.endpoint }}', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(data)
        });
        
        const result = await response.json();
        
        if (result.success) {
            window.location.href = '/dashboard';
        } else {
            alert(result.message);
        }
    } catch (error) {
        alert('An error occurred. Please try again.');
    }
});
</script>
{% endblock %}'''
    
    with open(os.path.join(templates_dir, 'auth.html'), 'w') as f:
        f.write(auth_template)
    
    # Dashboard template (UPDATED JAVASCRIPT FOR BUTTON RESET)
    dashboard_template = '''{% extends "base.html" %}

{% block content %}
<div class="max-w-6xl mx-auto">
    <div class="mb-8">
        <h2 class="text-3xl font-bold mb-2">Download Center</h2>
        <p class="opacity-75">Enter a YouTube URL to download video or audio</p>
    </div>
    
    <div class="card border rounded-xl p-8 mb-8">
        <form id="downloadForm" action="{{ url_for('download') }}" method="POST">
            <div class="grid md:grid-cols-4 gap-4 items-end">
                <div class="md:col-span-2">
                    <label class="block text-sm font-medium mb-2">YouTube URL</label>
                    <input type="url" id="youtubeUrl" name="url" placeholder="https://www.youtube.com/watch?v=..." 
                            class="w-full px-4 py-3 card border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500" required>
                </div>
                
                <div>
                    <label class="block text-sm font-medium mb-2">Download Type</label>
                    <select id="downloadType" name="type" class="w-full px-4 py-3 card border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500">
                        <option value="video">Video (MP4)</option>
                        <option value="audio">Audio (MP3)</option>
                    </select>
                </div>
                
                <button type="submit" id="downloadBtn" 
                        class="bg-blue-600 text-white px-6 py-3 rounded-lg hover:bg-blue-700 transition-colors">
                    <i class="fas fa-download mr-2"></i>
                    Download
                </button>
            </div>
        </form>
    </div>
    
    <div class="card border rounded-xl p-8">
        <div class="flex justify-between items-center mb-6">
            <h3 class="text-2xl font-bold">Your Downloads</h3>
            <button onclick="checkStatus()" class="card border px-4 py-2 rounded-lg hover:opacity-80 transition-opacity">
                <i class="fas fa-refresh mr-2"></i>
                Refresh
            </button>
        </div>
        
        <div id="downloadsList">
            {% if downloads %}
                {% for download in downloads %}
                <div class="card border rounded-lg p-4 mb-4">
                    <div class="flex justify-between items-center">
                        <div class="flex-1">
                            <h4 class="font-semibold mb-1">{{ download.title }}</h4>
                            <div class="text-sm opacity-75">
                                <span class="mr-4">
                                    <i class="fas fa-{{ 'video' if download.download_type == 'video' else 'music' }} mr-1"></i>
                                    {{ download.download_type.title() }}
                                </span>
                                <span class="mr-4">{{ download.created_at.strftime('%Y-%m-%d %H:%M') }}</span>
                                <span class="status-{{ download.status }}">
                                    {% if download.status == 'completed' %}
                                        <i class="fas fa-check-circle text-green-600 mr-1"></i>
                                    {% elif download.status == 'downloading' %}
                                        <i class="fas fa-spinner fa-spin text-blue-600 mr-1"></i>
                                    {% elif download.status == 'failed' %}
                                        <i class="fas fa-times-circle text-red-600 mr-1"></i>
                                    {% else %}
                                        <i class="fas fa-clock text-yellow-600 mr-1"></i>
                                    {% endif %}
                                    {{ download.status.title() }}
                                </span>
                            </div>
                        </div>
                        {% if download.status == 'completed' and download.filename %}
                        <a href="{{ url_for('download_file', filename=download.filename) }}" 
                            class="bg-green-600 text-white px-4 py-2 rounded-lg hover:bg-green-700 transition-colors">
                            <i class="fas fa-download mr-1"></i>
                            Download
                        </a>
                        {% endif %}
                    </div>
                </div>
                {% endfor %}
            {% else %}
                <div class="text-center py-12 opacity-75">
                    <i class="fas fa-inbox text-4xl mb-4"></i>
                    <p>No downloads yet. Start by entering a YouTube URL above.</p>
                </div>
            {% endif %}
        </div>
    </div>
</div>

<script>
document.getElementById('downloadForm').addEventListener('submit', function() {
    const btn = document.getElementById('downloadBtn');
    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin mr-2"></i>Preparing Download...';
});

// Reset the button state on page load
document.addEventListener('DOMContentLoaded', function() {
    const btn = document.getElementById('downloadBtn');
    const flashMessagesDiv = document.querySelector('.mb-4'); // Check for flash message container

    // If there were flash messages (meaning a redirect just happened after an action)
    // or if the button is still disabled for some reason, reset it.
    if (flashMessagesDiv || btn.disabled) {
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-download mr-2"></i>Download';
    }

    // Call checkStatus immediately on load to update the download list
    checkStatus();
});


async function checkStatus() {
    try {
        const response = await fetch('/check_status');
        const result = await response.json();
        
        const downloadsList = document.getElementById('downloadsList');
        
        if (result.downloads.length === 0) {
            downloadsList.innerHTML = `
                <div class="text-center py-12 opacity-75">
                    <i class="fas fa-inbox text-4xl mb-4"></i>
                    <p>No downloads yet. Start by entering a YouTube URL above.</p>
                </div>
            `;
        } else {
            downloadsList.innerHTML = result.downloads.map(download => `
                <div class="card border rounded-lg p-4 mb-4">
                    <div class="flex justify-between items-center">
                        <div class="flex-1">
                            <h4 class="font-semibold mb-1">${download.title}</h4>
                            <div class="text-sm opacity-75">
                                <span class="mr-4">
                                    <i class="fas fa-${download.type === 'video' ? 'video' : 'music'} mr-1"></i>
                                    ${download.type.charAt(0).toUpperCase() + download.type.slice(1)}
                                </span>
                                <span class="mr-4">${download.created_at}</span>
                                <span class="status-${download.status}">
                                    ${getStatusIcon(download.status)}
                                    ${download.status.charAt(0).toUpperCase() + download.status.slice(1)}
                                </span>
                            </div>
                        </div>
                        ${download.status === 'completed' && download.filename ? 
                            `<a href="/download_file/${download.filename}" 
                               class="bg-green-600 text-white px-4 py-2 rounded-lg hover:bg-green-700 transition-colors">
                                <i class="fas fa-download mr-1"></i>
                                Download
                            </a>` : ''}
                    </div>
                </div>
            `).join('');
        }
    } catch (error) {
        console.error('Error checking status:', error);
    }
}

function getStatusIcon(status) {
    switch(status) {
        case 'completed':
            return '<i class="fas fa-check-circle text-green-600 mr-1"></i>';
        case 'downloading':
            return '<i class="fas fa-spinner fa-spin text-blue-600 mr-1"></i>';
        case 'failed':
            return '<i class="fas fa-times-circle text-red-600 mr-1"></i>';
        default:
            return '<i class="fas fa-clock text-yellow-600 mr-1"></i>';
    }
}

// Auto-refresh downloads every 10 seconds
setInterval(checkStatus, 10000);
</script>
{% endblock %}'''
    
    with open(os.path.join(templates_dir, 'dashboard.html'), 'w') as f:
        f.write(dashboard_template)
    
    return "Templates created successfully!"

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    
    # Create templates on first run (or if they don't exist)
    # You might want to remove this line after the first successful run
    # if you intend to manually edit your HTML templates.
    create_templates() 
    
    app.run(debug=True, host='0.0.0.0', port=5000)