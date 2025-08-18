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

# === NUEVO: imports para la conversión MP3->WAV (si llega MP3) ===
from pydub import AudioSegment
import io
import tempfile

# (opcional) asegura que pydub use ffmpeg del PATH
os.environ.setdefault("FFMPEG_BINARY", "ffmpeg")
try:
    # En muchas Raspberry Pi, ffmpeg está en /usr/bin/ffmpeg
    if os.path.exists("/usr/bin/ffmpeg"):
        AudioSegment.converter = "/usr/bin/ffmpeg"
except Exception:
    pass

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

# === NUEVO: helper para reproducir SIEMPRE WAV, convirtiendo si hace falta ===
def play_response_bytes_as_wav(resp_bytes: bytes, content_type: str | None):
    """
    Normaliza cualquier respuesta (wav/mp3) a WAV PCM S16 mono 16k
    y la reproduce con aplay. Evita 'estática' por formatos raros.
    """
    ct = (content_type or "").lower()
    tmp_path = None

    # Heurística de formato
    if "wav" in ct:
        fmt = "wav"
    elif "mp3" in ct or "mpeg" in ct:
        fmt = "mp3"
    else:
        # sniff rápido: ID3 -> MP3, RIFF....WAVE -> WAV
        if resp_bytes[:3] == b"ID3":
            fmt = "mp3"
        elif resp_bytes[:4] == b"RIFF" and resp_bytes[8:12] == b"WAVE":
            fmt = "wav"
        else:
            fmt = "mp3"  # mejor asumir mp3 si no sabemos

    try:
        # 1) Decodifica a AudioSegment
        audio = AudioSegment.from_file(io.BytesIO(resp_bytes), format=fmt)

        # 2) Normaliza a 16 kHz, mono, 16-bit PCM (S16_LE)
        audio = (audio
                 .set_frame_rate(16000)
                 .set_channels(1)
                 .set_sample_width(2))  # 2 bytes = 16-bit

        # 3) Exporta a WAV temporal y reproduce
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            # fuerza pcm_s16le por si acaso
            audio.export(tmp.name, format="wav", parameters=["-acodec", "pcm_s16le"])
            tmp_path = tmp.name

        play_audio(tmp_path)

    except Exception as e:
        print(f"[WARN] decode/normalize failed: {e}; intento WAV directo")
        # Último recurso: tocar lo que llegó como si fuera WAV
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(resp_bytes)
            tmp_path = tmp.name
        play_audio(tmp_path)
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
               pass

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
                # === CAMBIO: en lugar de guardar a disco y reproducir "respuesta.wav",
                # usamos el helper que convierte a WAV si hace falta y reproduce.
                play_response_bytes_as_wav(response.content, response.headers.get("Content-Type"))
                print("[MCP] Got response audio, played.")

                if expect_followup:
                    state = CONVERSATION_ACTIVE
                    last_activity_time = time.time()
                else:
                    state = IDLE
            else:
                print(f"[MCP ERROR] Status: {response.status_code}")
        except Exception as e:
            print(f"[MCP ERROR] {e}")

        try:
            os.remove(file_to_upload)
        except Exception:
            pass
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