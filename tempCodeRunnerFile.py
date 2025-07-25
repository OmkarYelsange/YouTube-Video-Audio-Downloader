from flask import Flask, render_template, request, jsonify, send_file, session, redirect, url_for, flash, after_this_request
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
import yt_dlp
import os
import uuid
from datetime import datetime
import threading
import time # <--- ADDED THIS IMPORT
from werkzeug.security import generate_password_hash, check_password_hash
import tempfile
import shutil

app = Flask(__name__)
# IMPORTANT: Change this to a strong, unique secret key!
app.config['SECRET_KEY'] = '21b73249a34e893eefc6c7efa744fde53ae334627c613a83feef32a575bbff25'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:////var/data/youtube_downloader.db' # Ensure this is correct for Render persistent disk
# --- MODIFIED UPLOAD_FOLDER TO USE PERSISTENT DISK ---
app.config['UPLOAD_FOLDER'] = '/var/data/downloads' # This folder will now store permanent downloads on the persistent disk

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Create downloads directory if it doesn't exist
# This will create /var/data/downloads if /var/data is the persistent mount
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
    # --- MODIFIED YDL_OPTS FOR AUDIO ---
            ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': output_template,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
                'noplaylist': True,
                'sleep_interval': 5, # Add a delay between requests
                'fragment_retries': 5, # Retry failed fragments
                'max_downloads': 1, # Process one download at a time
                'verbose': True, # <--- ADD THIS LINE
                'no_warnings': False, # <--- Consider adding this as well to see warnings
            }
            download_extension = 'mp3'
        else: # video
            output_template = os.path.join(temp_dir, f"{unique_id}.%(ext)s")
            # --- MODIFIED YDL_OPTS FOR VIDEO ---
            ydl_opts = {
                'format': 'bestvideo[height<=720]+bestaudio/best[height<=720]/best', # Prioritize 720p, merge video+audio
                'outtmpl': output_template,
                'noplaylist': True,
                'merge_output_format': 'mp4', # Ensure output is mp4 after merging
                'sleep_interval': 5, # Add a delay between requests
                'fragment_retries': 5, # Retry failed fragments
                'max_downloads': 1, # Process one download at a time
                'verbose': True, # <--- ADD THIS LINE
                'no_warnings': False, # <--- Consider adding this as well to see warnings
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