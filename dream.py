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
VOICE_MCP_URL = "https://dreamincode-abgjgwgfckbqergq.eastus-01.azurewebsites.net/voice_mcp"

USER_ID = 3   # <--- assign elderly user ID here
CHANNELS = 1
RATE = 16000
RECORD_SECONDS = 5
QUEUE = queue.Queue()

# Medicine schedule
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

# Pre-buffer 1 sec before trigger
pre_buffer_frames = deque(maxlen=int(RATE / porcupine.frame_length * 1))

def save_audio_from_frames(filename, frames):
    with wave.open(filename, 'wb') as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(pa.get_sample_size(pyaudio.paInt16))
        wf.setframerate(RATE)
        wf.writeframes(b''.join(frames))

def play_audio(filename):
    os.system(f"aplay {filename} >/dev/null 2>&1")

def mcp_worker():
    """Worker: sends audio to MCP endpoint and plays response."""
    global state, last_activity_time
    while True:
        file_to_upload, expect_followup = QUEUE.get()
        try:
            with open(file_to_upload, 'rb') as f:
                response = requests.post(
                    VOICE_MCP_URL,
                    files={'audio': (file_to_upload, f, 'audio/wav')},
                    data={"usuario_id": USER_ID, "lang": "es"},  # <--- USER_ID used here
                    timeout=60
                )

            if response.status_code == 200:
                out_file = "respuesta.wav"
                with open(out_file, "wb") as f:
                    f.write(response.content)

                print("[MCP] Got response audio, playing...")
                play_audio(out_file)
                os.remove(out_file)

                if expect_followup:
                    state = CONVERSATION_ACTIVE
                    last_activity_time = time.time()
                else:
                    state = IDLE
            else:
                print(f"[MCP ERROR] Status: {response.status_code}")
        except Exception as e:
            print(f"[MCP ERROR] {e}")

        os.remove(file_to_upload)
        QUEUE.task_done()

def reminder_scheduler():
    """Medication reminders thread."""
    global state
    while True:
        now = datetime.now()
        for med in MEDICATIONS:
            if now.hour == med["hour"] and now.minute == med["minute"]:
                print(f"[REMINDER] Time to take {med['name']}")
                reminder_file = "reminder.wav"

                response = requests.post(
                    VOICE_MCP_URL,
                    data={"usuario_id": USER_ID, "lang": "en"},  # <--- USER_ID used here too
                    files={'audio': ("reminder.wav", b"", 'audio/wav')}
                )

                if response.status_code == 200:
                    with open(reminder_file, "wb") as f:
                        f.write(response.content)
                    play_audio(reminder_file)
                    os.remove(reminder_file)

                state = WAITING_FOR_CONFIRMATION
        time.sleep(60)

# Start threads
threading.Thread(target=mcp_worker, daemon=True).start()
# threading.Thread(target=reminder_scheduler, daemon=True).start()

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
                save_audio_from_frames("wake_audio.wav", frames)
                QUEUE.put(("wake_audio.wav", True))

        elif state in (CONVERSATION_ACTIVE, WAITING_FOR_CONFIRMATION):
            if time.time() - last_activity_time > 10:
                state = IDLE
            else:
                frames = []
                for _ in range(int(RATE / porcupine.frame_length * RECORD_SECONDS)):
                    frames.append(stream.read(porcupine.frame_length, exception_on_overflow=False))
                save_audio_from_frames("followup.wav", frames)
                QUEUE.put(("followup.wav", True))
                last_activity_time = time.time()

except KeyboardInterrupt:
    print("\nShutting down...")
finally:
    stream.stop_stream()
    stream.close()
    pa.terminate()
    porcupine.delete()
