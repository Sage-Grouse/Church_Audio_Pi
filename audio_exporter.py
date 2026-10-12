# -*- coding: utf-8 -*-
"""
# Description:  Convert church service audio on a Raspberry Pi 5 from
#               a wav file on a usb drive to mp3, upload via sftp, segment 
#               audio, normalize,reduce noise (declick/denoise), check for 
#               existing remote files, and email logs.
# Author: Carl Rudeen, St John's Lutheran Church, American Falls, ID, USA
# License: MIT (see LICENSE)
# Version: 3.3.0 
"""

import os
import sys
import glob
import posixpath
import argparse
import psutil
import time
import threading
import smtplib
import subprocess
import tempfile
from email.message import EmailMessage
from datetime import date, datetime, timedelta

# GUI Libraries
import tkinter as tk
from tkinter import ttk, messagebox

# Audio and Network Libraries
from pydub import AudioSegment, effects
from pydub.silence import detect_nonsilent
import paramiko
from dotenv import load_dotenv

# === Load environment variables ===
load_dotenv()

# Configuration
# Site-specific settings come from the .env file (see .env.example).
def _require_env(name):
    value = os.getenv(name)
    if not value:
        sys.exit(f"ERROR: {name} is not set. Copy .env.example to .env and fill it in.")
    return value

SFTP_HOST = _require_env("SFTP_HOST")
SFTP_PORT = int(os.getenv("SFTP_PORT", "22"))
SFTP_REMOTE_DIR = _require_env("SFTP_REMOTE_DIR")
LATEST_FILENAME = os.getenv("LATEST_FILENAME", "Current_Service.mp3")
EMAIL_LIST = [a.strip() for a in _require_env("EMAIL_TO").split(",") if a.strip()]
ORG_NAME = os.getenv("ORG_NAME", "Church")            # used in email subjects, window title, mp3 tags
FOLDER_PREFIX = os.getenv("FOLDER_PREFIX", "Service_")  # year folder name: <prefix><year>
DAYS_TO_CHECK = int(os.getenv("DAYS_TO_CHECK", "6"))
MIN_FILE_SIZE_BYTES = 1024
MUSIC_SUBDIR = "Music"  # final mp3 goes into a Music folder, not loose in a drive root

# The mp3 goes to a SEPARATE, permanently-connected output drive
# instead of the Yamaha's recording drive — filling up the Yamaha drive with
# mp3s (on top of the day's WAV) was causing its recording buffer to skip.
# This path is a fixed mountpoint set up via /etc/fstab (see SETUP_NOTES.md),
# not the drive that gets inserted/removed each week.
OUTPUT_PATH = os.getenv("OUTPUT_PATH", "/mnt/church_music")

parser = argparse.ArgumentParser()
parser.add_argument("--input", default=None, help="Path the USB drive is mounted at")
parser.add_argument("--force", action="store_true", help="Overwrite even if a matching file already exists on the server")
args, _ = parser.parse_known_args()

p = psutil.Process(os.getpid())
try:
    p.nice(-10)  # Gives the audio rendering more CPU cycles on the Pi 5
except Exception:
    pass  # Requires elevated privileges; skip if not permitted

# --- Logger Class to capture print statements for email ---
class EmailLogger:
    def __init__(self):
        self.logs = []
    def log(self, message):
        timestamp = datetime.now().strftime('%H:%M:%S')
        full_msg = f"[{timestamp}] {message}"
        print(full_msg)
        self.logs.append(full_msg)
    def get_content(self):
        return "\n".join(self.logs)

logger = EmailLogger()

# --- Helper Functions ---
def generate_date_patterns(days_to_check=DAYS_TO_CHECK):
    date_patterns, filename_prefixes = [], []
    today = date.today()
    current_year = today.strftime('%Y')
    for i in range(days_to_check):
        target_date = today - timedelta(days=i)
        prefix = target_date.strftime('%Y%m%d')
        filename_prefixes.append(prefix)
        date_patterns.append(prefix + "*.wav")
    return date_patterns, filename_prefixes, current_year

def send_email_report(subject, content):
    msg = EmailMessage()
    msg.set_content(content)
    msg['Subject'] = f"{ORG_NAME} Audio: {subject}"
    msg['From'] = os.getenv("EMAIL_USER")
    msg['To'] = ", ".join(EMAIL_LIST)

    try:
        # NOTE: Gmail requires a 16-character "App Password" here, not your
        # normal account password. Create one at myaccount.google.com/apppasswords
        # (requires 2-Step Verification to be turned on for the account first).
        # A regular account password will fail to authenticate.
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(os.getenv("EMAIL_USER"), os.getenv("EMAIL_PASS"))
            server.send_message(msg)
        logger.log("Email report sent.")
    except Exception as e:
        logger.log(f"Failed to send email: {e}")

def declick_denoise(input_wav_path: str) -> str:
    tmp_fd, tmp_path = tempfile.mkstemp(suffix="_cleaned.wav")
    os.close(tmp_fd)
    cmd = [
        "ffmpeg", "-y", "-i", input_wav_path,
        "-af", "adeclick,afftdn=nf=-25",
        tmp_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.log(f"ffmpeg declick/denoise failed, continuing with original audio: {result.stderr[-500:]}")
        os.remove(tmp_path)
        return input_wav_path
    return tmp_path

def reduce_noise(segment: AudioSegment) -> AudioSegment:
    # Secondary pass — trims rumble/hiss beyond what ffmpeg caught.
    filtered = segment.high_pass_filter(100)
    filtered = filtered.low_pass_filter(8000)
    return filtered

# --- Main Processing Class (GUI) ---
class AudioApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"{ORG_NAME} Audio")
        self.root.geometry("640x420")

        self.label = ttk.Label(root, text="System Ready\nInsert Yamaha USB Drive", font=("Arial", 18), padding=20, wraplength=560)
        self.label.pack()

        self.progress = ttk.Progressbar(root, orient="horizontal", length=500, mode="determinate")
        self.progress.pack(pady=20)

        self.time_label = ttk.Label(root, text="Elapsed: 00:00", font=("Arial", 14))
        self.time_label.pack()

        self.overwrite_var = tk.BooleanVar(value=True)
        self.overwrite_check = tk.Checkbutton(
            root, text="Overwrite if already on server",
            variable=self.overwrite_var,
            font=("Arial", 14, "bold"),
            padx=15, pady=10
        )
        self.overwrite_check.pack(pady=5)

        button_row = ttk.Frame(root)
        button_row.pack(pady=15)

        self.btn = ttk.Button(button_row, text="START PROCESSING", command=self.start_thread)
        self.btn.grid(row=0, column=0, padx=10)

        self.exit_btn = ttk.Button(button_row, text="EXIT", command=self.confirm_exit)
        self.exit_btn.grid(row=0, column=1, padx=10)

        style = ttk.Style()
        style.configure('TButton', font=('Arial', 18, 'bold'), padding=10)

    def confirm_exit(self):
        if self.btn['state'] == 'disabled':
            msg = "Processing is currently running. Exiting now may leave things in an incomplete state. Are you sure you want to exit?"
        else:
            msg = "Are you sure you want to exit?"
        if messagebox.askyesno("Exit", msg):
            self.root.destroy()

    def start_thread(self):
        self.btn.config(state="disabled")
        self.process_start_time = time.time()
        self.timer_id = self.root.after(1000, self.update_timer)
        threading.Thread(target=self.run_main_logic, daemon=True).start()

    def update_timer(self):
        elapsed = int(time.time() - self.process_start_time)
        mins, secs = divmod(elapsed, 60)
        hours, mins = divmod(mins, 60)
        text = f"Elapsed: {hours:02d}:{mins:02d}:{secs:02d}" if hours else f"Elapsed: {mins:02d}:{secs:02d}"
        self.time_label.config(text=text)
        self.timer_id = self.root.after(1000, self.update_timer)

    def stop_timer(self):
        if getattr(self, "timer_id", None):
            self.root.after_cancel(self.timer_id)
            self.timer_id = None

    def update_ui(self, msg, val):
        self.label.config(text=msg)
        self.progress['value'] = val
        self.root.update_idletasks()

    def run_main_logic(self):
        start_time = time.time()
        logger.log("Pi 5 Audio Pipeline Started")

        self.progress.config(mode="indeterminate")
        self.progress.start(15)

        try:
            # 1. Setup Patterns
            date_patterns, filename_prefixes, year = generate_date_patterns()

            # 2. Find USB Drive
            self.update_ui("Searching for USB Drive...", 0)
            usb_path = args.input  # prefer the path systemd already mounted
            all_files = []

            if usb_path:
                for pattern in date_patterns:
                    all_files.extend(glob.glob(os.path.join(usb_path, pattern)))
            else:
                # Fallback for manual/local testing without --input
                partitions = psutil.disk_partitions()
                for part in partitions:
                    if 'removable' in part.opts or '/media/' in part.mountpoint:
                        for pattern in date_patterns:
                            matches = glob.glob(os.path.join(part.mountpoint, pattern))
                            if matches:
                                all_files.extend(matches)
                                usb_path = part.mountpoint

            if not all_files:
                raise Exception("No service files found on USB. Check date.")

            if not os.path.ismount(OUTPUT_PATH):
                raise Exception(
                    f"Output drive not found at {OUTPUT_PATH}. Check that it's "
                    f"plugged in and the /etc/fstab entry is correct."
                )
            if not os.access(OUTPUT_PATH, os.W_OK):
                raise Exception(f"Output drive at {OUTPUT_PATH} is not writable.")

            current_file = max(all_files, key=os.path.getmtime)
            filename_prefix = os.path.basename(current_file)[:8]
            output_mp3 = f"{filename_prefix}.mp3"

            # 3. Check SFTP Server (Save time if already uploaded)
            self.update_ui(f"Checking server for {output_mp3}...", 0)
            transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
            transport.connect(username=os.getenv("SFTP_USERNAME"), password=os.getenv("SFTP_PASSWORD"))
            sftp = paramiko.SFTPClient.from_transport(transport)

            remote_path = posixpath.join(SFTP_REMOTE_DIR, output_mp3)
            try:
                sftp.stat(remote_path)
                if args.force or self.overwrite_var.get():
                    logger.log(f"File {output_mp3} exists but overwrite was requested — overwriting.")
                else:
                    logger.log(f"File {output_mp3} exists. Skipping conversion.")
                    self.progress.stop()
                    self.progress.config(mode="determinate")
                    self.update_ui("File already on server. Task skipped.", 100)
                    send_email_report("SKIPPED - File Exists", logger.get_content())
                    return
            except IOError:
                logger.log("File not on server. Beginning Render.")

            # Switch to determinate progress
            self.progress.stop()
            self.progress.config(mode="determinate")

            # 4. Declick / denoise pass
            self.update_ui("Removing pops, static & rustling...", 15)
            cleaned_wav_path = declick_denoise(current_file)

            # 5. Audio Processing (segment, filter, normalize)
            self.update_ui("Loading audio...", 25)
            audio = AudioSegment.from_file(cleaned_wav_path, format="wav")
            nonsilent = detect_nonsilent(audio, min_silence_len=5000, silence_thresh=-45)

            segments = []
            total_segments = max(len(nonsilent), 1)
            for i, (start_ms, end_ms) in enumerate(nonsilent):
                chunk = audio[max(0, start_ms - 500): min(len(audio), end_ms + 500)]
                if len(chunk) > 500:
                    cleaned = reduce_noise(chunk)
                    segments.append(effects.normalize(cleaned))

                pct = 25 + int((i + 1) / total_segments * 35)  # 25% -> 60%
                self.update_ui(f"Processing segment {i + 1} of {total_segments}...", pct)

            if cleaned_wav_path != current_file:
                os.remove(cleaned_wav_path)  # clean up temp file from declick_denoise()

            # Reassemble with crossfades
            combined = AudioSegment.silent(duration=3500)
            for i, seg in enumerate(segments):
                combined += seg
                if i < len(segments) - 1:
                    combined = combined.append(AudioSegment.silent(duration=1500), crossfade=100)

            # CHANGED: removed the 1.1x speedup step — final_audio
            # is now just the reassembled, filtered audio at normal speed.
            # To re-enable a 10% speed-up, uncomment these lines and remove
            # the "final_audio = combined" line below them:
            # logger.log("Applying 1.1x speedup...")
            # self.update_ui("Applying 10% speed increase...", 60)
            # final_audio = combined.speedup(playback_speed=1.1)

            final_audio = combined

            # 6. Export to the SEPARATE output drive, into Music/<FOLDER_PREFIX><year>/
            music_dir = os.path.join(OUTPUT_PATH, MUSIC_SUBDIR, f"{FOLDER_PREFIX}{year}")
            os.makedirs(music_dir, exist_ok=True)
            self.update_ui("Exporting MP3 to USB...", 80)
            album = f"{ORG_NAME} {year}"
            local_output = os.path.join(music_dir, output_mp3)
            final_audio.export(local_output, format="mp3", bitrate="128k", tags={'Album': album, 'Artist': ORG_NAME})

            # 7. Upload via SFTP
            self.update_ui("Uploading to Church Website...", 90)
            sftp.put(local_output, remote_path)
            sftp.put(local_output, posixpath.join(SFTP_REMOTE_DIR, LATEST_FILENAME))

            # 8. Cleanup USB (Keep newest 2 dated recordings, delete the rest)
            # Keeping more than a few files on the usb drive will cause the Yamaha TF1 to
            # over buffer and skip
            def _recording_date(path):
                try:
                    return datetime.strptime(os.path.basename(path)[:8], "%Y%m%d")
                except ValueError:
                    return datetime.min

            wavs = sorted(glob.glob(os.path.join(usb_path, '*.wav')), key=_recording_date)
            for old_wav in wavs[:-2]:
                os.remove(old_wav)
                logger.log(f"Cleaned up: {os.path.basename(old_wav)}")

            total_duration = round(time.time() - start_time)
            _mins, _secs = divmod(total_duration, 60)
            _hours, _mins = divmod(_mins, 60)
            duration_str = f"{_hours:02d}:{_mins:02d}:{_secs:02d}" if _hours else f"{_mins:02d}:{_secs:02d}"
            self.update_ui(f"Success! Processed in {duration_str}", 100)
            logger.log(f"Total processing time: {duration_str}")
            send_email_report("SUCCESS", logger.get_content())

        except Exception as e:
            logger.log(f"ERROR: {str(e)}")
            try:
                self.progress.stop()
                self.progress.config(mode="determinate")
            except Exception:
                pass
            self.update_ui("Error. See email for details.", 0)
            send_email_report("FAILED", logger.get_content())
            messagebox.showerror("System Error", str(e))
        finally:
            try:
                sftp.close()
                transport.close()
            except Exception:
                pass
            self.stop_timer()
            self.btn.config(state="normal")

if __name__ == "__main__":
    root = tk.Tk()
    # If using a touchscreen, this helps the GUI fit properly
    # root.attributes('-fullscreen', True)  # Uncomment if you want it full screen
    app = AudioApp(root)
    root.mainloop()
