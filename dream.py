import pvporcupine
import pyaudio
import struct
import wave
import requests
import threading
import queue
import time

# === CONFIG ===
ACCESS_KEY = "YOUR_ACCESS_KEY_HERE"
WAKE_WORD = "porcupine"  # or any built-in word

AUDIO_FILENAME = 'wake_audio.wav'
UPLOAD_URL = 'http://localhost:5000/upload'
# === AUDIO SETTINGS ===
CHANNELS = 1
RATE = 16000
CHUNK = 512
QUEUE = queue.Queue()

# === WAKE WORD ===
porcupine = pvporcupine.create(
    access_key=ACCESS_KEY,
    keywords=[WAKE_WORD]
)
pa = pyaudio.PyAudio()
stream = pa.open(
    rate=porcupine.sample_rate,
    channels=1,
    format=pyaudio.paInt16,
    input=True,
    frames_per_buffer=porcupine.frame_length,
)

def save_audio(filename, record_seconds=5):
    wf = wave.open(filename, 'wb')
    wf.setnchannels(CHANNELS)
    wf.setsampwidth(pa.get_sample_size(pyaudio.paInt16))
    wf.setframerate(RATE)
    
    stream = pa.open(format=pyaudio.paInt16, channels=CHANNELS, rate=RATE, input=True, frames_per_buffer=CHUNK)
    frames = []
    
    for _ in range(int(RATE / CHUNK * record_seconds)):
        data = stream.read(CHUNK)
        frames.append(data)
    
    stream.stop_stream()
    stream.close()
    
    wf.writeframes(b''.join(frames))
    wf.close()

def upload_worker():
    while True:
        file_to_upload = QUEUE.get()
        try:
            with open(file_to_upload, 'rb') as f:
                response = requests.post(UPLOAD_URL, files={'file': f})
            print(f'Uploaded: {file_to_upload}, Status: {response.status_code}')
        except Exception as e:
            print(f'Upload failed: {e}')
            QUEUE.put(file_to_upload)  # Retry
        QUEUE.task_done()
        time.sleep(2)

threading.Thread(target=upload_worker, daemon=True).start()

print("Listening for wake word...")

try:
    while True:
        pcm = stream.read(porcupine.frame_length)
        pcm = struct.unpack_from("h" * porcupine.frame_length, pcm)

        keyword_index = porcupine.process(pcm)
        if keyword_index >= 0:
            print("Wake word detected!")
            save_audio(AUDIO_FILENAME)
            QUEUE.put(AUDIO_FILENAME)
except KeyboardInterrupt:
    pass
finally:
    stream.stop_stream()
    stream.close()
    pa.terminate()
    porcupine.delete()
