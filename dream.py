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
import subprocess  # para reproducir audio de espera en loop (ahora 1 sola vez)
import statistics

# -----------------------------------------------------------------------------
# Configuración
# -----------------------------------------------------------------------------

ACCESS_KEY = "heQRVcJzahp/QdflX+KJRkOr6yvkclzaAKK6fY1NEKdYwtowZocbOg=="
WAKE_WORD = "porcupine"

VOICE_MCP_URL = "https://dreamincode-abgjgwgfckbqergq.eastus-01.azurewebsites.net/voice_mcp"
REMINDER_TTS_URL = "https://dreamincode-abgjgwgfckbqergq.eastus-01.azurewebsites.net/reminder_tts"

USER_ID = 3            # id del adulto mayor
CHANNELS = 1
RATE = 16000          # destino para normalización / guardado WAV
QUEUE = queue.Queue()

# VAD / conversación
MIN_SPEECH_MS = 500            # mínimo de voz acumulada para considerar "frase" válida
TRAILING_SILENCE_MS = 700      # silencio para cortar al final
MAX_UTTERANCE_S = 8            # tope duro por utterance
FOLLOWUP_LISTEN_WINDOW_S = 10  # ventana para esperar que el usuario empiece a hablar
FOLLOWUP_COOLDOWN_S = 0.8      # anti rebote tras enviar un followup

# Estados
IDLE = "IDLE"
WAITING_FOR_SPEECH = "WAITING_FOR_SPEECH"      # esperando voz del usuario
CONVERSATION_ACTIVE = "CONVERSATION_ACTIVE"    # hubo respuesta TTS; podemos escuchar follow-ups
WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"
state = IDLE
last_activity_time = time.time()
last_followup_sent_at = 0.0

# Mediciones de tiempo
last_rec_started_at = 0.0

# Agenda de ejemplo (no usada si el thread está comentado)
MEDICATIONS = [
    {"hour": 8, "minute": 0, "name": "blood pressure pill"},
    {"hour": 20, "minute": 0, "name": "cholesterol tablet"},
]

# -----------------------------------------------------------------------------
# pydub/ffmpeg
# -----------------------------------------------------------------------------
os.environ.setdefault("FFMPEG_BINARY", "ffmpeg")
try:
    if os.path.exists("/usr/bin/ffmpeg"):
        AudioSegment.converter = "/usr/bin/ffmpeg"
except Exception:
    pass

# -----------------------------------------------------------------------------
# Audio local de espera
# -----------------------------------------------------------------------------
try:
    BASE_DIR = os.path.dirname(os.path.abspath(_file_))
except NameError:
    BASE_DIR = os.getcwd()

WAIT_AUDIO_PATH = os.path.join(BASE_DIR, "PrefabAudios", "waitResponse.wav")

def play_audio(filename):
    os.system(f"aplay '{filename}' >/dev/null 2>&1")

def _normalize_wait_wav(src_path: str) -> str:
    audio = AudioSegment.from_file(src_path)
    audio = audio.set_frame_rate(16000).set_channels(1).set_sample_width(2)
    out_path = os.path.join(tempfile.gettempdir(), "waitResponse__16k_mono.wav")
    audio.export(out_path, format="wav", parameters=["-acodec", "pcm_s16le"])
    return out_path

WAIT_AUDIO_PLAY_PATH = None
if os.path.exists(WAIT_AUDIO_PATH):
    try:
        WAIT_AUDIO_PLAY_PATH = _normalize_wait_wav(WAIT_AUDIO_PATH)
        print(f"[WAIT] Normalized -> {WAIT_AUDIO_PLAY_PATH}")
    except Exception as e:
        WAIT_AUDIO_PLAY_PATH = WAIT_AUDIO_PATH
        print(f"[WAIT] Normalization failed, using raw file: {e}")
else:
    print(f"[WAIT] Archivo no encontrado: {WAIT_AUDIO_PATH}")

# --- NUEVO: reproducción de espera SOLO UNA VEZ (no loop) ---
def play_wait_once(file_path: str):
    """Devuelve el proceso de aplay para poder cortarlo si la respuesta llega antes."""
    if not os.path.exists(file_path):
        return None
    try:
        return subprocess.Popen(
            ["aplay", "-q", file_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
    except Exception as e:
        print(f"[WAIT] error reproduciendo: {e}")
        return None

# -----------------------------------------------------------------------------
# Utilidades WAV locales
# -----------------------------------------------------------------------------

def save_audio_from_frames(filename, frames, sample_rate, sample_width=2, channels=1):
    """Guarda frames PCM (bytes) como WAV."""
    with wave.open(filename, 'wb') as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(b''.join(frames))

# -----------------------------------------------------------------------------
# Decodificación/normalización de respuestas (backend -> reproducción rápida)
# -----------------------------------------------------------------------------

def looks_like_mp3(buf: bytes) -> bool:
    if len(buf) < 2:
        return False
    if buf[:3] == b"ID3":
        return True
    return buf[0] == 0xFF and (buf[1] & 0xE0) == 0xE0

def looks_like_wav(buf: bytes) -> bool:
    return len(buf) >= 12 and buf[:4] == b"RIFF" and buf[8:12] == b"WAVE"

# --- NUEVO: reproductor “fast-path” (evita conversiones) ---
def _write_temp(suffix: str, data: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        return tmp.name

def play_response_bytes_fast(resp_bytes: bytes, content_type: str | None):
    """
    Reproduce con mínima transformación:
      - WAV -> aplay
      - MP3 -> mpg123
      - si no se reconoce -> último recurso pydub->wav
    """
    ct = (content_type or "").lower().strip()
    tmp_path = None
    try:
        # fast path por content-type
        if "audio/wav" in ct or "audio/x-wav" in ct or ct.endswith("/wav") or "wav" in ct:
            tmp_path = _write_temp(".wav", resp_bytes)
            os.system(f"aplay -q '{tmp_path}' >/dev/null 2>&1")
            return
        if "audio/mpeg" in ct or "mp3" in ct:
            tmp_path = _write_temp(".mp3", resp_bytes)
            os.system(f"mpg123 -q '{tmp_path}' >/dev/null 2>&1")
            return

        # heurística por bytes
        if looks_like_wav(resp_bytes):
            tmp_path = _write_temp(".wav", resp_bytes)
            os.system(f"aplay -q '{tmp_path}' >/dev/null 2>&1")
            return
        if looks_like_mp3(resp_bytes):
            tmp_path = _write_temp(".mp3", resp_bytes)
            os.system(f"mpg123 -q '{tmp_path}' >/dev/null 2>&1")
            return

        # último recurso: convertir a wav 16k mono con pydub
        audio = AudioSegment.from_file(io.BytesIO(resp_bytes))
        audio = audio.set_frame_rate(16000).set_channels(1).set_sample_width(2)
        tmp_path = _write_temp(".wav", b"")
        audio.export(tmp_path, format="wav", parameters=["-acodec", "pcm_s16le"])
        os.system(f"aplay -q '{tmp_path}' >/dev/null 2>&1")
    except Exception as e:
        print(f"[AUDIO] Fallback por error: {e}")
        tmp_path = _write_temp(".wav", resp_bytes)
        os.system(f"aplay -q '{tmp_path}' >/dev/null 2>&1")
    finally:
        if tmp_path:
            try: os.remove(tmp_path)
            except Exception: pass

# -----------------------------------------------------------------------------
# Inicialización de Porcupine + PyAudio y calibración de ruido
# -----------------------------------------------------------------------------

porcupine = pvporcupine.create(access_key=ACCESS_KEY, keywords=[WAKE_WORD])
SAMPLE_RATE = porcupine.sample_rate        # 16000
FRAME_LEN = porcupine.frame_length         # típicamente 512
FRAME_MS = int(1000 * FRAME_LEN / SAMPLE_RATE)

pa = pyaudio.PyAudio()
stream = pa.open(
    rate=SAMPLE_RATE,
    channels=CHANNELS,
    format=pyaudio.paInt16,
    input=True,
    frames_per_buffer=FRAME_LEN
)

def _rms_int16(pcm_bytes: bytes) -> float:
    if not pcm_bytes:
        return 0.0
    count = len(pcm_bytes) // 2
    if count == 0:
        return 0.0
    samples = struct.unpack("<" + "h"*count, pcm_bytes[:count*2])
    acc = 0
    for s in samples:
        acc += s*s
    return (acc / count) ** 0.5

def calibrate_noise(frames=50) -> float:
    vals = []
    for _ in range(frames):
        b = stream.read(FRAME_LEN, exception_on_overflow=False)
        vals.append(_rms_int16(b))
    med = statistics.median(vals)
    thr = max(300.0, med * 3.0)  # suelo 300 y 3x del piso
    print(f"[VAD] noise median={med:.1f} -> threshold={thr:.1f}")
    return thr

ENERGY_THRESHOLD = calibrate_noise()

# pre-buffer ~1s para la frase tras el wake-word
pre_buffer_frames = deque(maxlen=int(SAMPLE_RATE / FRAME_LEN * 1))

# -----------------------------------------------------------------------------
# Grabación controlada por VAD
# -----------------------------------------------------------------------------

def record_utterance_vad(prebuffer=None) -> tuple[list[bytes], float]:
    """
    Graba hasta detectar silencio de cola; requiere MIN_SPEECH_MS de voz.
    Retorna (frames, dur_seg). frames son bytes PCM S16LE a SAMPLE_RATE.
    """
    frames = []
    speech_ms = 0
    silence_ms = 0
    speech_started = False
    t0 = time.perf_counter()

    if prebuffer:
        frames.extend(list(prebuffer))

    while True:
        b = stream.read(FRAME_LEN, exception_on_overflow=False)
        frames.append(b)
        rms = _rms_int16(b)

        if rms >= ENERGY_THRESHOLD:
            speech_started = True
            speech_ms += FRAME_MS
            silence_ms = 0
        else:
            if speech_started:
                silence_ms += FRAME_MS

        elapsed = time.perf_counter() - t0
        if speech_started and speech_ms >= MIN_SPEECH_MS and silence_ms >= TRAILING_SILENCE_MS:
            break
        if elapsed >= MAX_UTTERANCE_S:
            break

    return frames, (time.perf_counter() - t0)

def wait_for_speech_then_record_vad(timeout_s=FOLLOWUP_LISTEN_WINDOW_S) -> tuple[list[bytes], float]:
    """
    Espera hasta detectar voz (RMS > threshold) y luego graba con VAD.
    Si no hay voz en 'timeout_s', retorna ([], 0.0).
    """
    t_start = time.perf_counter()
    while (time.perf_counter() - t_start) < timeout_s:
        b = stream.read(FRAME_LEN, exception_on_overflow=False)
        pre_buffer_frames.append(b)
        rms = _rms_int16(b)
        if rms >= ENERGY_THRESHOLD:
            frames, dur = record_utterance_vad(prebuffer=pre_buffer_frames)
            return frames, dur
    return [], 0.0

# -----------------------------------------------------------------------------
# Worker: envía audio al backend y reproduce la respuesta
# -----------------------------------------------------------------------------

def mcp_worker():
    global state, last_activity_time, last_followup_sent_at
    while True:
        file_to_upload, expect_followup = QUEUE.get()
        wait_proc = None
        try:
            # Reproduce audio de espera UNA sola vez
            if WAIT_AUDIO_PLAY_PATH and os.path.exists(WAIT_AUDIO_PLAY_PATH):
                wait_proc = play_wait_once(WAIT_AUDIO_PLAY_PATH)

            with open(file_to_upload, 'rb') as f:
                t0 = time.time()
                response = requests.post(
                    VOICE_MCP_URL,
                    files={'audio': (file_to_upload, f, 'audio/wav')},
                    data={"usuario_id": USER_ID, "lang": "es"},
                    timeout=60
                )
                rt = time.time() - t0

            # corta la espera si sigue sonando
            if wait_proc and (wait_proc.poll() is None):
                try: wait_proc.terminate()
                except Exception: pass

            if response.status_code == 200:
                print(f"[NET] round-trip {rt:.2f}s, content-type={response.headers.get('Content-Type')}")
                # Reproductor rápido sin conversiones pesadas
                play_response_bytes_fast(response.content, response.headers.get("Content-Type"))
                print("[MCP] Got response audio, played.")

                if expect_followup:
                    state = CONVERSATION_ACTIVE
                    last_activity_time = time.time()
                    last_followup_sent_at = 0.0
                else:
                    state = IDLE
            else:
                print(f"[MCP ERROR] Status: {response.status_code}")
        except Exception as e:
            print(f"[MCP ERROR] {e}")
            if wait_proc and (wait_proc.poll() is None):
                try: wait_proc.terminate()
                except Exception: pass
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
                        },
                        timeout=30,
                    )
                    rt = time.time() - t0
                    if r.status_code == 200:
                        print(f"[REMINDER] TTS ok ({rt:.2f}s) ct={r.headers.get('Content-Type')}")
                        play_response_bytes_fast(r.content, r.headers.get("Content-Type"))
                        state = WAITING_FOR_CONFIRMATION
                    else:
                        print(f"[REMINDER] HTTP {r.status_code}")
                except Exception as e:
                    print(f"[REMINDER] error: {e}")
        time.sleep(60)

# -----------------------------------------------------------------------------
# Lanzar threads
# -----------------------------------------------------------------------------

threading.Thread(target=mcp_worker, daemon=True).start()
# threading.Thread(target=reminder_scheduler, daemon=True).start()  # opcional

print("Listening for wake word...")

# -----------------------------------------------------------------------------
# Bucle principal
# -----------------------------------------------------------------------------

try:
    while True:
        try:
            pcm = stream.read(FRAME_LEN, exception_on_overflow=False)
        except Exception as e:
            print(f"[Stream Error] {e}")
            continue

        pre_buffer_frames.append(pcm)
        pcm_unpacked = struct.unpack_from("h" * (len(pcm)//2), pcm)

        if state == IDLE:
            keyword_index = porcupine.process(pcm_unpacked)
            if keyword_index >= 0:
                print("[DETECTED] Wake word")
                frames, dur = record_utterance_vad(prebuffer=pre_buffer_frames)
                print(f"[REC] wake dur={dur:.2f}s frames={len(frames)}")
                save_audio_from_frames("wake_audio.wav", frames, SAMPLE_RATE)
                print("[REC] archivo= wake_audio.wav; subiendo…")
                QUEUE.put(("wake_audio.wav", True))

        elif state in (CONVERSATION_ACTIVE, WAITING_FOR_CONFIRMATION):
            if last_followup_sent_at and (time.time() - last_followup_sent_at) < FOLLOWUP_COOLDOWN_S:
                continue

            frames, dur = wait_for_speech_then_record_vad(timeout_s=FOLLOWUP_LISTEN_WINDOW_S)
            if frames:
                print(f"[REC] follow-up dur={dur:.2f}s frames={len(frames)}")
                save_audio_from_frames("followup.wav", frames, SAMPLE_RATE)
                print("[REC] archivo= followup.wav; subiendo…")
                QUEUE.put(("followup.wav", True))
                last_activity_time = time.time()
                last_followup_sent_at = time.time()
            else:
                state = IDLE

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