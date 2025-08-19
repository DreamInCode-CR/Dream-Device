
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

# --- ENDPOINTS base y rutas específicas ---
BASE_URL         = "https://dreamincode-abgjgwgfckbqergq.eastus-01.azurewebsites.net"
VOICE_MCP_URL    = f"{BASE_URL}/voice_mcp"
REMINDER_TTS_URL = f"{BASE_URL}/reminder_tts"
MEDS_ALL_URL     = f"{BASE_URL}/meds/all"   # <- devuelve {"items":[...]}
TTS_URL          = f"{BASE_URL}/tts"

USER_ID = 3            # id del adulto mayor
CHANNELS = 1
RATE = 16000          # destino para normalización / guardado WAV
QUEUE = queue.Queue()

# VAD / conversación
MIN_SPEECH_MS = 500
TRAILING_SILENCE_MS = 700
MAX_UTTERANCE_S = 8
FOLLOWUP_LISTEN_WINDOW_S = 10
FOLLOWUP_COOLDOWN_S = 0.8

# Scheduler
CHECK_INTERVAL_S = 60           # revisión cada minuto
MATCH_TOLERANCE_MIN = 1         # tolerancia ±1 minuto para disparar
TEST_REMINDER_ON_START = False  # pon True para probar un recordatorio al arrancar

# Estados
IDLE = "IDLE"
WAITING_FOR_SPEECH = "WAITING_FOR_SPEECH"
CONVERSATION_ACTIVE = "CONVERSATION_ACTIVE"
WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"
state = IDLE
last_activity_time = time.time()
last_followup_sent_at = 0.0

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
    import shutil as _sh
    return _sh.which("mpg123") is not None

def play_response_bytes(resp_bytes: bytes, content_type: str | None):
    sniff_fmt = _sniff_fmt(resp_bytes)
    header_fmt = _fmt_from_header(content_type)
    fmt = sniff_fmt or header_fmt
    print(f"[AUDIO] sniff={sniff_fmt} header={header_fmt} -> using {fmt}")

    try:
        if fmt == "wav":
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
                audio = AudioSegment.from_file(io.BytesIO(resp_bytes), format="mp3")
                audio = audio.set_frame_rate(16000).set_channels(1).set_sample_width(2)
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                    audio.export(tmp.name, format="wav", parameters=["-acodec", "pcm_s16le"])
                    path = tmp.name
                play_wav(path)
                os.remove(path)

        else:
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
KEYWORD_PATH = os.path.join(WAKE_DIR, "Hey-Dream_en_raspberry-pi_v3_0_0.ppn")  # tu .ppn

porcupine = pvporcupine.create(
    access_key=ACCESS_KEY,
    keyword_paths=[KEYWORD_PATH],
    sensitivities=[0.65]
)
SAMPLE_RATE = porcupine.sample_rate
FRAME_LEN = porcupine.frame_length
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
    thr = max(300.0, med * 3.0)
    print(f"[VAD] noise median={med:.1f} -> threshold={thr:.1f}")
    return thr

ENERGY_THRESHOLD = calibrate_noise()

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
            # SINGLE SHOT de audio de “espere…”
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
                print(f"[MCP ERROR] Status: {response.status_code} body={response.text[:200]}")
        except Exception as e:
            print(f"[MCP ERROR] {e}")
        finally:
            try:
                os.remove(file_to_upload)
            except Exception:
                pass
            QUEUE.task_done()

# -----------------------------------------------------------------------------
# Scheduler de recordatorios (usa /meds/all + /reminder_tts)
# -----------------------------------------------------------------------------

FIRED_REMINDERS = set()
MEDICATIONS: list[dict] = []

def fetch_medications():
    """Carga todas las meds del usuario desde /meds/all."""
    global MEDICATIONS
    try:
        r = requests.get(MEDS_ALL_URL, params={"usuario_id": USER_ID}, timeout=10)
        if r.status_code == 200:
            payload = r.json()
            meds = payload.get("items", []) or payload.get("medications", [])
            if isinstance(meds, list):
                MEDICATIONS = meds
                print(f"[MEDS] Updated medications: {len(MEDICATIONS)} items")
            else:
                print(f"[MEDS] Unexpected payload: {payload.keys()}")
        else:
            print(f"[MEDS] Failed to fetch: {r.status_code} body={r.text[:200]}")
    except Exception as e:
        print(f"[MEDS] Error fetching medications: {e}")

def medication_refresher():
    while True:
        fetch_medications()
        time.sleep(600)

def _hhmm_to_minutes(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)

def _today_flag_key() -> str:
    # Devuelve 'Lunes'...'Domingo'
    return {
        "Monday": "Lunes",
        "Tuesday": "Martes",
        "Wednesday": "Miercoles",
        "Thursday": "Jueves",
        "Friday": "Viernes",
        "Saturday": "Sabado",
        "Sunday": "Domingo",
    }[datetime.now().strftime("%A")]

def _play_reminder_via_backend(med: dict, now_hhmm: str):
    """Llama a /reminder_tts con los datos completos (tu flujo de servidor)."""
    nombre = med.get("NombreMedicamento", "tu medicamento")
    dosis  = med.get("Dosis", "") or ""
    try:
        print(f"[AUTO] POST /reminder_tts medicamento='{nombre}' hora='{now_hhmm}'")
        r = requests.post(
            REMINDER_TTS_URL,
            json={
                "usuario_id": USER_ID,
                "auto": False,
                "medicamento": nombre,
                "dosis": dosis,
                "hora": now_hhmm
            },
            timeout=30
        )
        if r.status_code == 200:
            print(f"[AUTO] ok ct={r.headers.get('Content-Type')}")
            play_response_bytes(r.content, r.headers.get("Content-Type"))
        else:
            print(f"[AUTO] HTTP {r.status_code} body={r.text[:200]}")
    except Exception as e:
        print(f"[AUTO] error: {e}")

def reminder_scheduler():
    """Evalúa cada minuto y dispara recordatorios que coincidan."""
    while True:
        now = datetime.now()
        now_hhmm = now.strftime("%H:%M")
        today_key = _today_flag_key()
        now_min = now.hour * 60 + now.minute

        print(f"[SCHED] tick now={now.isoformat(timespec='seconds')} ({today_key}) — meds={len(MEDICATIONS)}")

        for med in MEDICATIONS:
            try:
                mid = med.get("MedicamentoID") or med.get("NombreMedicamento")
                hora = (med.get("HoraToma") or "").strip()
                activo = bool(med.get("Activo", True))

                # Logs de evaluación
                print(f"  [CHK] id={mid} activo={activo} HoraToma='{hora}' "
                      f"dias={{Lu:{med.get('Lunes')}, Ma:{med.get('Martes')}, Mi:{med.get('Miercoles')}, "
                      f"Ju:{med.get('Jueves')}, Vi:{med.get('Viernes')}, Sa:{med.get('Sabado')}, Do:{med.get('Domingo')}}}")

                if not activo or not hora or not med.get(today_key, False):
                    continue

                med_min = _hhmm_to_minutes(hora)
                diff = abs(med_min - now_min)

                if diff <= MATCH_TOLERANCE_MIN:
                    key = (now.date().isoformat(), hora, str(mid))
                    if key in FIRED_REMINDERS:
                        print(f"  [SKIP] already fired {key}")
                        continue

                    FIRED_REMINDERS.add(key)
                    print(f"  [FIRE] {mid} @ {hora} (diff={diff}min) -> /reminder_tts")
                    _play_reminder_via_backend(med, now_hhmm)

            except Exception as e:
                print(f"[SCHED] error with med {med}: {e}")

        time.sleep(CHECK_INTERVAL_S)

# -----------------------------------------------------------------------------
# Lanzar threads
# -----------------------------------------------------------------------------

def mcp_worker_thread():
    threading.Thread(target=mcp_worker, daemon=True).start()

def sched_threads():
    fetch_medications()
    threading.Thread(target=medication_refresher, daemon=True).start()
    threading.Thread(target=reminder_scheduler, daemon=True).start()

def self_test_reminder():
    """Opcional: dispara una prueba al arrancar para verificar audio."""
    if not TEST_REMINDER_ON_START:
        return
    fake_med = {
        "MedicamentoID": "TEST",
        "NombreMedicamento": "pastilla de prueba",
        "Dosis": "1",
        "Activo": True,
        "Lunes": True, "Martes": True, "Miercoles": True, "Jueves": True,
        "Viernes": True, "Sabado": True, "Domingo": True,
        "HoraToma": datetime.now().strftime("%H:%M"),
    }
    print("[TEST] Lanzando recordatorio de prueba…")
    _play_reminder_via_backend(fake_med, fake_med["HoraToma"])

# -----------------------------------------------------------------------------
# Bucle principal (wake word + VAD)
# -----------------------------------------------------------------------------

def main():
    global state, last_followup_sent_at

    # 1) Threads
    mcp_worker_thread()
    sched_threads()
    self_test_reminder()

    print("Listening for wake word...")

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
                last_followup_sent_at = time.time()
            else:
                state = IDLE

if __name__ == "_main_":
    try:
        main()
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