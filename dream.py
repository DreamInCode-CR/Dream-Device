import os
import io
import time
import wave
import queue
import struct
import threading
from collections import deque
from datetime import datetime

import pvporcupine
import pyaudio
import requests

# === Audio decode / convert ===
from pydub import AudioSegment
import tempfile
import subprocess  # <-- añadido para reproducir audio de espera en loop

# -----------------------------------------------------------------------------
# Configuración
# -----------------------------------------------------------------------------

ACCESS_KEY = "heQRVcJzahp/QdflX+KJRkOr6yvkclzaAKK6fY1NEKdYwtowZocbOg=="
WAKE_WORD = "porcupine"

VOICE_MCP_URL = "https://dreamincode-abgjgwgfckbqergq.eastus-01.azurewebsites.net/voice_mcp"
REMINDER_TTS_URL = "https://dreamincode-abgjgwgfckbqergq.eastus-01.azurewebsites.net/reminder_tts"

USER_ID = 3            # id del adulto mayor
CHANNELS = 1
RATE = 16000
RECORD_SECONDS = 5
QUEUE = queue.Queue()

# Ruta absoluta a la carpeta del proyecto (donde está este script)
BASE_DIR = os.path.dirname(os.path.abspath(_file_))

# Audio local de espera (dentro del repo)
WAIT_AUDIO_PATH = os.path.join(BASE_DIR, "PrefabAudios", "waitResponse.wav") 

# Mediciones de tiempo
last_rec_started_at = 0.0

# Agenda de ejemplo (no usada si el thread está comentado)
MEDICATIONS = [
    {"hour": 8, "minute": 0, "name": "blood pressure pill"},
    {"hour": 20, "minute": 0, "name": "cholesterol tablet"},
]

# Estados
IDLE = "IDLE"
CONVERSATION_ACTIVE = "CONVERSATION_ACTIVE"
WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"
state = IDLE
last_activity_time = time.time()

# -----------------------------------------------------------------------------
# pydub/ffmpeg
# -----------------------------------------------------------------------------
# (opcional) asegurarnos de usar /usr/bin/ffmpeg si existe
os.environ.setdefault("FFMPEG_BINARY", "ffmpeg")
try:
    if os.path.exists("/usr/bin/ffmpeg"):
        AudioSegment.converter = "/usr/bin/ffmpeg"
except Exception:
    pass

# -----------------------------------------------------------------------------
# Utilidades de audio local
# -----------------------------------------------------------------------------

def save_audio_from_frames(filename, frames):
    """Guarda frames PCM S16 a un WAV mono 16k."""
    pa = pyaudio.PyAudio()
    try:
        with wave.open(filename, 'wb') as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(pa.get_sample_size(pyaudio.paInt16))
            wf.setframerate(RATE)
            wf.writeframes(b''.join(frames))
    finally:
        pa.terminate()

def play_audio(filename):
    # aplay maneja perfecto PCM S16LE 16k mono
    os.system(f"aplay '{filename}' >/dev/null 2>&1")

# -----------------------------------------------------------------------------
# Sniff de formato y reproducción robusta
# -----------------------------------------------------------------------------

def looks_like_mp3(buf: bytes) -> bool:
    if len(buf) < 2:
        return False
    # ID3 tag
    if buf[:3] == b"ID3":
        return True
    # Frame sync: 0xFF seguido de byte con 3 bits altos = 111xxxxx
    return buf[0] == 0xFF and (buf[1] & 0xE0) == 0xE0

def looks_like_wav(buf: bytes) -> bool:
    # RIFF .... WAVE
    return len(buf) >= 12 and buf[:4] == b"RIFF" and buf[8:12] == b"WAVE"

def play_response_bytes_as_wav(resp_bytes: bytes, content_type: str):
    """
    Normaliza cualquier respuesta (wav/mp3) a WAV PCM S16 16k mono
    y la reproduce con aplay. Evita 'estática' por tipos engañosos.
    """
    ct = (content_type or "").lower()

    # 1) Sniff manda (no nos fiamos del Content-Type)
    if looks_like_wav(resp_bytes):
        guess_fmt = "wav"
    elif looks_like_mp3(resp_bytes):
        guess_fmt = "mp3"
    else:
        guess_fmt = "wav" if "wav" in ct else "mp3"

    print(f"[AUDIO] CT={ct or '-'} guess={guess_fmt} bytes={len(resp_bytes)}")

    def decode_as(fmt: str) -> AudioSegment:
        return AudioSegment.from_file(io.BytesIO(resp_bytes), format=fmt)

    tmp_path = None
    try:
        # Intento principal
        try:
            audio = decode_as(guess_fmt)
        except Exception as e1:
            # Reintento con el otro formato
            alt_fmt = "mp3" if guess_fmt == "wav" else "wav"
            print(f"[AUDIO] decode as {guess_fmt} falló ({e1}); reintento como {alt_fmt}")
            audio = decode_as(alt_fmt)

        # Normalizar a 16 kHz, mono, 16-bit PCM
        audio = audio.set_frame_rate(16000).set_channels(1).set_sample_width(2)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            # fuerza pcm_s16le
            audio.export(tmp.name, format="wav", parameters=["-acodec", "pcm_s16le"])
            tmp_path = tmp.name

        play_audio(tmp_path)

    except Exception as e:
        print(f"[AUDIO] ERROR irrecuperable: {e}; toco 'tal cual' por último recurso")
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

# -----------------------------------------------------------------------------
# Audio de espera (loop hasta que llegue la respuesta del servidor)
# -----------------------------------------------------------------------------

def start_wait_loop(file_path: str) -> threading.Event | None:
    """
    Reproduce file_path en loop con 'aplay' hasta que returns_stop_event.set() sea llamado.
    Devuelve el Event para parar. Si no existe el archivo, devuelve None.
    """
    if not os.path.exists(file_path):
        print(f"[WAIT] archivo no encontrado: {file_path}")
        return None

    stop = threading.Event()

    def _loop():
        while not stop.is_set():
            try:
                p = subprocess.Popen(
                    ["aplay", "-q", file_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                # Espera no bloqueante con posibilidad de cortar
                while p.poll() is None:
                    if stop.is_set():
                        try:
                            p.terminate()
                        except Exception:
                            pass
                        break
                    time.sleep(0.1)
            except Exception as e:
                print(f"[WAIT] error reproduciendo: {e}")
                break

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return stop

# -----------------------------------------------------------------------------
# Worker: envía audio al backend y reproduce la respuesta
# -----------------------------------------------------------------------------

def mcp_worker():
    global state, last_activity_time
    while True:
        file_to_upload, expect_followup = QUEUE.get()
        wait_stop = None
        try:
            # Arranca el audio de espera si existe
            if os.path.exists(WAIT_AUDIO_PATH):
                wait_stop = start_wait_loop(WAIT_AUDIO_PATH)

            with open(file_to_upload, 'rb') as f:
                t0 = time.time()
                response = requests.post(
                    VOICE_MCP_URL,
                    files={'audio': (file_to_upload, f, 'audio/wav')},
                    data={"usuario_id": USER_ID, "lang": "es"},
                    timeout=60
                )
                rt = time.time() - t0

            # Detener audio de espera en cuanto llega respuesta
            if wait_stop:
                wait_stop.set()

            if response.status_code == 200:
                print(f"[NET] round-trip {rt:.2f}s, content-type={response.headers.get('Content-Type')}")
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
            # Asegurar detener audio de espera si hubo error
            if wait_stop:
                wait_stop.set()
        finally:
            try:
                os.remove(file_to_upload)
            except Exception:
                pass
            QUEUE.task_done()

# -----------------------------------------------------------------------------
# Scheduler de recordatorios (usa /reminder_tts) - opcional
# -----------------------------------------------------------------------------

def reminder_scheduler():
    """Ejemplo de uso del endpoint /reminder_tts."""
    global state
    while True:
        now = datetime.now()
        for med in MEDICATIONS:
            if now.hour == med["hour"] and now.minute == med["minute"]:

                print(f"[REMINDER] Time to take {med['name']}")
                try:
                    t0 = time.time()
                    r = requests.post(
                        REMINDER_TTS_URL,
                        json={
                            "usuario_id": USER_ID,
                            "medicamento": med["name"],
                            "dosis": "",
                            "hora": f"{now.hour:02d}:{now.minute:02d}",
                            # usa el tz_offset que prefieras; aquí omitido
                        },
                        timeout=30,
                    )
                    rt = time.time() - t0
                    if r.status_code == 200:
                        print(f"[REMINDER] TTS ok ({rt:.2f}s) ct={r.headers.get('Content-Type')}")
                        play_response_bytes_as_wav(r.content, r.headers.get("Content-Type"))
                        state = WAITING_FOR_CONFIRMATION
                    else:
                        print(f"[REMINDER] HTTP {r.status_code}")
                except Exception as e:
                    print(f"[REMINDER] error: {e}")

        time.sleep(60)

# -----------------------------------------------------------------------------
# Inicialización de audio + Porcupine
# -----------------------------------------------------------------------------

porcupine = pvporcupine.create(access_key=ACCESS_KEY, keywords=[WAKE_WORD])

pa = pyaudio.PyAudio()
stream = pa.open(
    rate=porcupine.sample_rate,
    channels=CHANNELS,
    format=pyaudio.paInt16,
    input=True,
    frames_per_buffer=porcupine.frame_length
)

# pre-buffer de ~1 s antes del trigger
pre_buffer_frames = deque(maxlen=int(RATE / porcupine.frame_length * 1))

# -----------------------------------------------------------------------------
# Lanzar threads
# -----------------------------------------------------------------------------

threading.Thread(target=mcp_worker, daemon=True).start()
# threading.Thread(target=reminder_scheduler, daemon=True).start()  # <-- actívalo si quieres

print("Listening for wake word...")

# -----------------------------------------------------------------------------
# Bucle principal
# -----------------------------------------------------------------------------

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
                last_rec_started_at = time.time()
                for _ in range(int(RATE / porcupine.frame_length * RECORD_SECONDS)):
                    frames.append(stream.read(porcupine.frame_length, exception_on_overflow=False))
                rec_dur = time.time() - last_rec_started_at
                print(f"[REC] dur={rec_dur:.2f}s frames={len(frames)}")

                save_audio_from_frames("wake_audio.wav", frames)
                print("[REC] archivo= wake_audio.wav; subiendo…")
                QUEUE.put(("wake_audio.wav", True))

        elif state in (CONVERSATION_ACTIVE, WAITING_FOR_CONFIRMATION):
            if time.time() - last_activity_time > 10:
                state = IDLE
            else:
                frames = []
                last_rec_started_at = time.time()
                for _ in range(int(RATE / porcupine.frame_length * RECORD_SECONDS)):
                    frames.append(stream.read(porcupine.frame_length, exception_on_overflow=False))
                rec_dur = time.time() - last_rec_started_at
                print(f"[REC] (follow-up) dur={rec_dur:.2f}s frames={len(frames)}")

                save_audio_from_frames("followup.wav", frames)
                print("[REC] archivo= followup.wav; subiendo…")
                QUEUE.put(("followup.wav", True))
                last_activity_time = time.time()

except KeyboardInterrupt:
    print("\nShutting down...")
finally:
    try:
        stream.stop_stream()
        stream.close()
    except Exception:
        pass
    pa.terminate()
    try:
        porcupine.delete()
    except Exception:
        pass