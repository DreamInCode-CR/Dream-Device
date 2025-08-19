import os
import io
import time
import wave
import queue
import struct
import threading
import shutil
from collections import deque
from datetime import datetime

import pvporcupine
import pyaudio
import requests

# === Audio decode / convert ===
from pydub import AudioSegment
import tempfile
import statistics

# -----------------------------------------------------------------------------
# Configuración
# -----------------------------------------------------------------------------

ACCESS_KEY = "heQRVcJzahp/QdflX+KJRkOr6yvkclzaAKK6fY1NEKdYwtowZocbOg=="


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
WAITING_FOR_SPEECH = "WAITING_FOR_SPEECH"
CONVERSATION_ACTIVE = "CONVERSATION_ACTIVE"
WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"
state = IDLE
last_activity_time = time.time()
last_followup_sent_at = 0.0

# Mediciones de tiempo
last_rec_started_at = 0.0

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
# Audio local de espera (una sola vez por petición)
# -----------------------------------------------------------------------------
try:
    BASE_DIR = os.path.dirname(os.path.abspath(_file_))
except NameError:
    BASE_DIR = os.getcwd()

WAIT_AUDIO_PATH = os.path.join(BASE_DIR, "PrefabAudios", "waitResponse.wav")

def play_wav(path: str):
    os.system(f"aplay -q '{path}' >/dev/null 2>&1")

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
# Reproducción robusta de respuestas del backend (sin estática)
# -----------------------------------------------------------------------------

def _fmt_from_header(ct: str | None) -> str | None:
    if not ct:
        return None
    c = ct.lower()
    if "wav" in c or "wave" in c:
        return "wav"
    if "mpeg" in c or "mp3" in c:
        return "mp3"
    return None

def _sniff_fmt(b: bytes) -> str | None:
    if len(b) >= 12 and b[:4] == b"RIFF" and b[8:12] == b"WAVE":
        return "wav"
    if len(b) >= 2 and (b[:3] == b"ID3" or (b[0] == 0xFF and (b[1] & 0xE0) == 0xE0)):
        return "mp3"
    return None

def _have_mpg123() -> bool:
    return shutil.which("mpg123") is not None

def play_response_bytes(resp_bytes: bytes, content_type: str | None):
    """
    Robust audio playback:
    - Sniff bytes first to detect actual format (wav/mp3).
    - If mismatch with Content-Type, prefer sniff.
    - Always decode with pydub when uncertain.
    """
    sniff_fmt = _sniff_fmt(resp_bytes)
    header_fmt = _fmt_from_header(content_type)
    fmt = sniff_fmt or header_fmt

    print(f"[AUDIO] sniff={sniff_fmt} header={header_fmt} -> using {fmt}")

    try:
        if fmt == "wav":
            # trust only if sniff says WAV
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp.write(resp_bytes)
                path = tmp.name
            try:
                play_wav(path)
            finally:
                os.remove(path)

        elif fmt == "mp3":
            if _have_mpg123():
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                    tmp.write(resp_bytes)
                    path = tmp.name
                try:
                    os.system(f"mpg123 -q '{path}' >/dev/null 2>&1")
                finally:
                    os.remove(path)
            else:
                # decode to WAV with pydub
                audio = AudioSegment.from_file(io.BytesIO(resp_bytes), format="mp3")
                audio = audio.set_frame_rate(16000).set_channels(1).set_sample_width(2)
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                    audio.export(tmp.name, format="wav", parameters=["-acodec", "pcm_s16le"])
                    path = tmp.name
                play_wav(path)
                os.remove(path)

        else:
            # fallback: force decode with pydub (handles wrong headers)
            audio = AudioSegment.from_file(io.BytesIO(resp_bytes))
            audio = audio.set_frame_rate(16000).set_channels(1).set_sample_width(2)
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                audio.export(tmp.name, format="wav", parameters=["-acodec", "pcm_s16le"])
                path = tmp.name
            play_wav(path)
            os.remove(path)

    except Exception as e:
        print(f"[AUDIO] Playback failed: {e}")
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp:
            tmp.write(resp_bytes)
            print(f"[AUDIO] Dumped raw bytes for debug: {tmp.name}")

# -----------------------------------------------------------------------------
# Inicialización de Porcupine + PyAudio y calibración de ruido
# -----------------------------------------------------------------------------

WAKE_DIR = os.path.join(BASE_DIR, "Wakewords")
KEYWORD_PATH = os.path.join(WAKE_DIR, "Hey-Dream_en_raspberry-pi_v3_0_0.ppn")  

porcupine = pvporcupine.create(
    access_key=ACCESS_KEY,
    keyword_paths=[KEYWORD_PATH],
    sensitivities=[0.65]   # 0–1 (más alto = más sensible = más falsos positivos)
)
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
        try:
            # Audio de espera en "one shot" (no loop)
            if WAIT_AUDIO_PLAY_PATH and os.path.exists(WAIT_AUDIO_PLAY_PATH):
                threading.Thread(target=play_wav, args=(WAIT_AUDIO_PLAY_PATH,), daemon=True).start()

            with open(file_to_upload, 'rb') as f:
                t0 = time.time()
                response = requests.post(
                    VOICE_MCP_URL,
                    files={'audio': (file_to_upload, f, 'audio/wav')},
                    data={"usuario_id": USER_ID, "lang": "es"},
                    timeout=60
                )
                rt = time.time() - t0

            if response.status_code == 200:
                print(f"[NET] round-trip {rt:.2f}s, content-type={response.headers.get('Content-Type')}")
                play_response_bytes(response.content, response.headers.get("Content-Type"))
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
        finally:
            try:
                os.remove(file_to_upload)
            except Exception:
                pass
            QUEUE.task_done()

# -----------------------------------------------------------------------------
# Scheduler de recordatorios (usa /reminder_tts) - opcional
# -----------------------------------------------------------------------------

FIRED_REMINDERS = set()  

MEDICATIONS = [] 
def fetch_medications():
    global MEDICATIONS
    try:
        r = requests.get(
            f"{REMINDER_TTS_URL}/meds/all",
            params={"usuario_id": USER_ID},
            timeout=10
        )
        if r.status_code == 200:
            meds = r.json().get("medications", [])
            if isinstance(meds, list):
                MEDICATIONS = meds
                print(f"[MEDS] Updated medications: {len(MEDICATIONS)} items")
            else:
                print("[MEDS] Unexpected payload shape")
        else:
            print(f"[MEDS] Failed to fetch, status {r.status_code}")
    except Exception as e:
        print(f"[MEDS] Error fetching medications: {e}")


def medication_refresher():
    while True:
        fetch_medications()
        time.sleep(600)  # 10 minutes

def speak(text: str):
    try:
        # If your backend expects a different JSON schema, adjust here.
        r = requests.post(
            REMINDER_TTS_URL,
            json={"usuario_id": USER_ID, "texto": text},
            timeout=30
        )
        if r.status_code == 200:
            play_response_bytes(r.content, r.headers.get("Content-Type"))
        else:
            print(f"[SPEAK] HTTP {r.status_code}: {r.text[:120]}")
    except Exception as e:
        print(f"[SPEAK] error: {e}")

import datetime

def reminder_scheduler():
    while True:
        now = datetime.datetime.now()
        weekday_en = now.strftime("%A")         # e.g. 'Monday'
        weekday_key = {
            "Monday": "Lunes",
            "Tuesday": "Martes",
            "Wednesday": "Miercoles",
            "Thursday": "Jueves",
            "Friday": "Viernes",
            "Saturday": "Sabado",
            "Sunday": "Domingo",
        }[weekday_en]
        today = now.date()
        today_iso = today.isoformat()
        current_time = now.strftime("%H:%M")

        for med in MEDICATIONS:
            try:
                if not med.get("Activo", True):
                    continue

                # Rango de fechas (permitir nulos)
                start_s = med.get("FechaInicio")
                end_s   = med.get("FechaHasta")
                if start_s:
                    start = datetime.date.fromisoformat(start_s)
                    if today < start:
                        continue
                if end_s:
                    end = datetime.date.fromisoformat(end_s)
                    if today > end:
                        continue

                # Día de la semana
                if not med.get(weekday_key, False):
                    continue

                # Hora exacta
                if med.get("HoraToma") != current_time:
                    continue

                # Evitar repetir dentro del mismo minuto
                med_id = med.get("MedicamentoID") or med.get("NombreMedicamento")
                key = (today_iso, current_time, str(med_id))
                if key in FIRED_REMINDERS:
                    continue
                FIRED_REMINDERS.add(key)

                # Armar texto
                name = med.get("NombreMedicamento", "tu medicamento")
                dose = med.get("Dosis", "")
                instructions = med.get("Instrucciones", "")
                dose_part = f", dosis {dose}" if dose else ""
                instr_part = f". {instructions}" if instructions else ""
                text = f"Es hora de tomar {name}{dose_part}{instr_part}"

                speak(text)

            except Exception as e:
                print(f"[REMINDER] Error processing med {med.get('MedicamentoID')}: {e}")

        time.sleep(60)  # check every minute



# -----------------------------------------------------------------------------
# Lanzar threads
# -----------------------------------------------------------------------------

threading.Thread(target=mcp_worker, daemon=True).start()

fetch_medications()

threading.Thread(target=medication_refresher, daemon=True).start()
threading.Thread(target=reminder_scheduler, daemon=True).start()

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