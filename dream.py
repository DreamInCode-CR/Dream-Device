import pvporcupine
import pyaudio
import struct
import wave
import requests
import threading
import queue
import time
import os
from collections import deque
from datetime import datetime

# === CONFIG ===
ACCESS_KEY = "heQRVcJzahp/QdflX+KJRkOr6yvkclzaAKK6fY1NEKdYwtowZocbOg=="
WAKE_WORD = "porcupine"
UPLOAD_URL = "http://localhost:5000/upload"
TTS_URL = "http://localhost:5000/tts"  # Where MCP sends back audio
AUDIO_FILENAME = "wake_audio.wav"
CHANNELS = 1
RATE = 16000
RECORD_SECONDS = 5
QUEUE = queue.Queue()

# Medicine schedule: hour/minute and medicine name
MEDICATIONS = [
    {"hour": 8, "minute": 0, "name": "blood pressure pill"},
    {"hour": 20, "minute": 0, "name": "cholesterol tablet"}
]

# States
IDLE = "IDLE"
CONVERSATION_ACTIVE = "CONVERSATION_ACTIVE"
WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"
state = IDLE
last_activity_time = time.time()

# === INIT PORCUPINE ===
porcupine = pvporcupine.create(access_key=ACCESS_KEY, keywords=[WAKE_WORD])

# === AUDIO INIT ===
pa = pyaudio.PyAudio()
stream = pa.open(
    rate=porcupine.sample_rate,
    channels=CHANNELS,
    format=pyaudio.paInt16,
    input=True,
    frames_per_buffer=porcupine.frame_length
)

# Pre-buffer for 1 sec before trigger
pre_buffer_frames = deque(maxlen=int(RATE / porcupine.frame_length * 1))

def save_audio_from_frames(filename, frames):
    with wave.open(filename, 'wb') as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(pa.get_sample_size(pyaudio.paInt16))
        wf.setframerate(RATE)
        wf.writeframes(b''.join(frames))

def play_audio(filename):
    os.system(f"aplay {filename} >/dev/null 2>&1")

def upload_worker():
    """Uploads recorded audio to MCP and plays TTS response."""
    global state, last_activity_time
    while True:
        file_to_upload, expect_followup = QUEUE.get()
        try:
            # Send audio to MCP
            with open(file_to_upload, 'rb') as f:
                response = requests.post(UPLOAD_URL, files={'file': f}, timeout=20)

            if response.status_code == 200:
                print(f"[UPLOAD] {file_to_upload} sent.")

                # Get TTS response from MCP
                r_tts = requests.get(TTS_URL, timeout=30)
                if r_tts.status_code == 200:
                    tts_file = "response.wav"
                    with open(tts_file, 'wb') as f:
                        f.write(r_tts.content)
                    play_audio(tts_file)
                    os.remove(tts_file)

                    if expect_followup:
                        state = CONVERSATION_ACTIVE
                        last_activity_time = time.time()
                    else:
                        state = IDLE
                else:
                    print("[ERROR] No TTS received.")
            else:
                print("[ERROR] MCP rejected file.")
        except Exception as e:
            print(f"[UPLOAD ERROR] {e}")

        os.remove(file_to_upload)
        QUEUE.task_done()

def reminder_scheduler():
    """Checks medication times and triggers reminders."""
    global state
    while True:
        now = datetime.now()
        for med in MEDICATIONS:
            if now.hour == med["hour"] and now.minute == med["minute"]:
                reminder_text = f"It's time to take your {med['name']}. Have you taken it yet?"
                print("[REMINDER]", reminder_text)
                # Save as TTS request to MCP (simulate TTS here)
                tts_file = "reminder.wav"
                # Assume MCP can generate this directly from text
                r_tts = requests.post(TTS_URL, data={"text": reminder_text})
                if r_tts.status_code == 200:
                    with open(tts_file, 'wb') as f:
                        f.write(r_tts.content)
                    play_audio(tts_file)
                    os.remove(tts_file)
                state = WAITING_FOR_CONFIRMATION
        time.sleep(60)

threading.Thread(target=upload_worker, daemon=True).start()
threading.Thread(target=reminder_scheduler, daemon=True).start()

print("Listening for wake word...")

try:
    while True:
        try:
            pcm = stream.read(porcupine.frame_length, exception_on_overflow=False)
        except Exception as e:
            print(f"[Stream Error] {e}")
            continue

        pcm_unpacked = struct.unpack_from("h" * porcupine.frame_length, pcm)
        pre_buffer_frames.append(pcm)

        if state == IDLE:
            keyword_index = porcupine.process(pcm_unpacked)
            if keyword_index >= 0:
                print("[DETECTED] Wake word")
                frames = list(pre_buffer_frames)
                for _ in range(int(RATE / porcupine.frame_length * RECORD_SECONDS)):
                    frames.append(stream.read(porcupine.frame_length, exception_on_overflow=False))
                save_audio_from_frames(AUDIO_FILENAME, frames)
                QUEUE.put((AUDIO_FILENAME, True))

        elif state in (CONVERSATION_ACTIVE, WAITING_FOR_CONFIRMATION):
            # Simple follow-up speech detection by timing
            if time.time() - last_activity_time > 10:
                state = IDLE
            else:
                # If voice detected, record & send
                frames = []
                for _ in range(int(RATE / porcupine.frame_length * RECORD_SECONDS)):
                    frames.append(stream.read(porcupine.frame_length, exception_on_overflow=False))
                save_audio_from_frames(AUDIO_FILENAME, frames)
                QUEUE.put((AUDIO_FILENAME, True))
                last_activity_time = time.time()

except KeyboardInterrupt:
    print("\nShutting down...")
finally:
    stream.stop_stream()
    stream.close()
    pa.terminate()
    porcupine.delete()
