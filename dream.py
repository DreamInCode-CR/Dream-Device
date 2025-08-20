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

API_BASE_URL       = "https://dreamincode-abgjgwgfckbqergq.eastus-01.azurewebsites.net"
VOICE_MCP_URL      = f"{API_BASE_URL}/voice_mcp"
REMINDER_TTS_URL   = f"{API_BASE_URL}/reminder_tts"
TTS_URL            = f"{API_BASE_URL}/tts"
MEDS_ALL_URL       = f"{API_BASE_URL}/meds/all"
STT_URL            = f"{API_BASE_URL}/stt"
MEDS_DUE_URL       = f"{API_BASE_URL}/meds/due"

# === APPOINTMENTS (NUEVO) ===
APPTS_DUE_URL     = f"{API_BASE_URL}/appts/due"
APPT_TTS_URL      = f"{API_BASE_URL}/appointment_tts"

USER_ID = 3
CHANNELS = 1
RATE = 16000
QUEUE = queue.Queue()

# VAD / conversación
MIN_SPEECH_MS = 500
TRAILING_SILENCE_MS = 700
MAX_UTTERANCE_S = 8
FOLLOWUP_LISTEN_WINDOW_S = 10
FOLLOWUP_COOLDOWN_S = 0.8

# Estados
IDLE = "IDLE"
WAITING_FOR_SPEECH = "WAITING_FOR_SPEECH"
CONVERSATION_ACTIVE = "CONVERSATION_ACTIVE"
WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"  # medicamentos
WAITING_FOR_APPT_CONFIRMATION = "WAITING_FOR_APPT_CONFIRMATION"  # citas (NUEVO)
state = IDLE
last_activity_time = time.time()
last_followup_sent_at = 0.0

# Candados para no repetir
REMINDER_LOCK_UNTIL = 0.0
REMINDER_LOCK_SECS  = 120.0
APPT_LOCK_UNTIL     = 0.0           # (NUEVO)
APPT_LOCK_SECS      = 180.0         # (NUEVO)

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
# Audio local de espera (one-shot)
# -----------------------------------------------------------------------------
try:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
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
# WAV utils
# -----------------------------------------------------------------------------
def save_audio_from_frames(filename, frames, sample_rate, sample_width=2, channels=1):
    with wave.open(filename, 'wb') as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(b''.join(frames))

# -----------------------------------------------------------------------------
# Playback robusto
# -----------------------------------------------------------------------------
def _fmt_from_header(ct: str | None) -> str | None:
    if not ct:
        return None
    c = ct.lower()
    if "wav" in c or "wave" in c: return "wav"
    if "mpeg" in c or "mp3" in c: return "mp3"
    return None

def _sniff_fmt(b: bytes) -> str | None:
    if len(b) >= 12 and b[:4] == b"RIFF" and b[8:12] == b"WAVE": return "wav"
    if len(b) >= 2 and (b[:3] == b"ID3" or (b[0] == 0xFF and (b[1] & 0xE0) == 0xE0)): return "mp3"
    return None

def _have_mpg123() -> bool:
    return shutil.which("mpg123") is not None

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
            try: play_wav(path)
            finally: os.remove(path)

        elif fmt == "mp3":
            if _have_mpg123():
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                    tmp.write(resp_bytes); path = tmp.name
                try: os.system(f"mpg123 -q '{path}' >/dev/null 2>&1")
                finally: os.remove(path)
            else:
                audio = AudioSegment.from_file(io.BytesIO(resp_bytes), format="mp3")
                audio = audio.set_frame_rate(16000).set_channels(1).set_sample_width(2)
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                    audio.export(tmp.name, format="wav", parameters=["-acodec", "pcm_s16le"]); path = tmp.name
                play_wav(path); os.remove(path)
        else:
            audio = AudioSegment.from_file(io.BytesIO(resp_bytes))
            audio = audio.set_frame_rate(16000).set_channels(1).set_sample_width(2)
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                audio.export(tmp.name, format="wav", parameters=["-acodec", "pcm_s16le"]); path = tmp.name
            play_wav(path); os.remove(path)

    except Exception as e:
        print(f"[AUDIO] Playback failed: {e}")

# -----------------------------------------------------------------------------
# TZ offset
# -----------------------------------------------------------------------------
def get_tz_offset_min() -> int:
    import datetime as _dt
    now = _dt.datetime.now(); utc = _dt.datetime.utcnow()
    return int(round((now - utc).total_seconds() / 60.0))

# -----------------------------------------------------------------------------
# Porcupine + audio in
# -----------------------------------------------------------------------------
WAKE_DIR = os.path.join(BASE_DIR, "Wakewords")
KEYWORD_PATH = os.path.join(WAKE_DIR, "Hey-Dream_en_raspberry-pi_v3_0_0.ppn")

porcupine = pvporcupine.create(
    access_key=ACCESS_KEY,
    keyword_paths=[KEYWORD_PATH],
    sensitivities=[0.65]
)
SAMPLE_RATE = porcupine.sample_rate
FRAME_LEN = porcupine.frame_length
FRAME_MS = int(1000 * FRAME_LEN / SAMPLE_RATE)

pa = pyaudio.PyAudio()
stream = pa.open(rate=SAMPLE_RATE, channels=1, format=pyaudio.paInt16, input=True, frames_per_buffer=FRAME_LEN)

def _rms_int16(pcm_bytes: bytes) -> float:
    if not pcm_bytes: return 0.0
    count = len(pcm_bytes)//2
    if count == 0: return 0.0
    samples = struct.unpack("<" + "h"*count, pcm_bytes[:count*2])
    acc = 0
    for s in samples: acc += s*s
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
# VAD helpers
# -----------------------------------------------------------------------------
def record_utterance_vad(prebuffer=None) -> tuple[list[bytes], float]:
    frames = []; speech_ms = 0; silence_ms = 0; speech_started = False
    t0 = time.perf_counter()
    if prebuffer: frames.extend(list(prebuffer))
    while True:
        b = stream.read(FRAME_LEN, exception_on_overflow=False)
        frames.append(b)
        rms = _rms_int16(b)
        if rms >= ENERGY_THRESHOLD:
            speech_started = True; speech_ms += FRAME_MS; silence_ms = 0
        else:
            if speech_started: silence_ms += FRAME_MS
        elapsed = time.perf_counter() - t0
        if speech_started and speech_ms >= MIN_SPEECH_MS and silence_ms >= TRAILING_SILENCE_MS: break
        if elapsed >= MAX_UTTERANCE_S: break
    return frames, (time.perf_counter() - t0)

def wait_for_speech_then_record_vad(timeout_s=FOLLOWUP_LISTEN_WINDOW_S) -> tuple[list[bytes], float]:
    t_start = time.perf_counter()
    while (time.perf_counter() - t_start) < timeout_s:
        b = stream.read(FRAME_LEN, exception_on_overflow=False)
        pre_buffer_frames.append(b)
        if _rms_int16(b) >= ENERGY_THRESHOLD:
            return record_utterance_vad(prebuffer=pre_buffer_frames)
    return [], 0.0

# -----------------------------------------------------------------------------
# STT + clasificación local
# -----------------------------------------------------------------------------
def stt_transcribe_wav(path: str) -> str:
    try:
        with open(path, "rb") as f:
            r = requests.post(
                STT_URL,
                files={'audio': (os.path.basename(path), f, 'audio/wav')},
                data={"usuario_id": USER_ID, "lang": "es"},
                timeout=30
            )
        if r.status_code == 200:
            return (r.json().get("transcripcion") or "").strip()
    except Exception as e:
        print(f"[STT] error: {e}")
    return ""

def classify_confirmation_local(text: str) -> str:
    t = (text or "").strip().lower()
    if not t: return "unsure"
    yes = ["sí","si","ya","claro","por supuesto","listo","hecho","me la tomé","me la tome","ya la tomé","ya la tome","ya lo hice","la tomé","la tome","asistiré","voy a ir","si iré","sí iré"]
    no  = ["no","todavía no","aún no","aun no","después","luego","más tarde","mas tarde","no la tomé","no la tome","no lo hice","no asistiré","no voy a ir","no puedo ir"]
    if any(w in t for w in yes): return "yes"
    if any(w in t for w in no):  return "no"
    return "unsure"

# -----------------------------------------------------------------------------
# Workers
# -----------------------------------------------------------------------------
def mcp_worker():
    global state, last_activity_time, last_followup_sent_at
    while True:
        file_to_upload, expect_followup = QUEUE.get()
        try:
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
            if response.status_code == 200:
                play_response_bytes(response.content, response.headers.get("Content-Type"))
                if expect_followup:
                    state = CONVERSATION_ACTIVE
                    last_activity_time = time.time()
                    last_followup_sent_at = 0.0
                else:
                    state = IDLE
        except Exception as e:
            print(f"[MCP ERROR] {e}")
        finally:
            try: os.remove(file_to_upload)
            except Exception: pass
            QUEUE.task_done()

def speak(text: str):
    try:
        r = requests.post(TTS_URL, json={"texto": text}, timeout=30)
        if r.status_code == 200:
            play_response_bytes(r.content, r.headers.get("Content-Type"))
    except Exception as e:
        print(f"[SPEAK] error: {e}")

# Poller MEDS (exacto)
def auto_reminder_poller():
    global state, REMINDER_LOCK_UNTIL
    while True:
        try:
            if time.time() < REMINDER_LOCK_UNTIL or state in (WAITING_FOR_CONFIRMATION, WAITING_FOR_APPT_CONFIRMATION):
                time.sleep(1); continue
            tz = get_tz_offset_min()
            g = requests.get(MEDS_DUE_URL, params={"usuario_id": USER_ID, "window_min": 0, "tz_offset_min": tz}, timeout=10)
            if g.status_code != 200:
                time.sleep(5); continue
            items = (g.json() or {}).get("items", [])
            if not items:
                time.sleep(5); continue
            it = items[0]
            payload = {
                "usuario_id": USER_ID, "auto": False,
                "medicamento": it.get("medicamento") or it.get("NombreMedicamento") or "",
                "dosis": it.get("dosis") or it.get("Dosis") or "",
                "hora": it.get("hora") or it.get("HoraToma") or "",
            }
            if not payload["medicamento"] or not payload["hora"]:
                time.sleep(5); continue
            r = requests.post(REMINDER_TTS_URL, json=payload, timeout=20)
            if r.status_code == 200:
                play_response_bytes(r.content, r.headers.get("Content-Type"))
                state = WAITING_FOR_CONFIRMATION
                REMINDER_LOCK_UNTIL = time.time() + REMINDER_LOCK_SECS
        except Exception as e:
            print(f"[AUTO] error: {e}")
        time.sleep(5)

# Poller APPOINTMENTS (NUEVO)
def appointment_poller():
    global state, APPT_LOCK_UNTIL
    while True:
        try:
            if time.time() < APPT_LOCK_UNTIL or state in (WAITING_FOR_CONFIRMATION, WAITING_FOR_APPT_CONFIRMATION):
                time.sleep(1); continue
            tz = get_tz_offset_min()
            g = requests.get(APPTS_DUE_URL, params={"usuario_id": USER_ID, "window_min": 0, "tz_offset_min": tz}, timeout=10)
            if g.status_code != 200:
                time.sleep(5); continue
            items = (g.json() or {}).get("items", [])
            if not items:
                time.sleep(5); continue
            it = items[0]
            payload = {
                "usuario_id": USER_ID, "auto": False,
                "titulo": it.get("titulo") or "",
                "hora":   it.get("hora") or "",
                "lugar":  it.get("lugar") or "",
                "doctor": it.get("doctor") or "",
            }
            if not payload["titulo"] or not payload["hora"]:
                time.sleep(5); continue
            r = requests.post(APPT_TTS_URL, json=payload, timeout=20)
            if r.status_code == 200:
                play_response_bytes(r.content, r.headers.get("Content-Type"))
                state = WAITING_FOR_APPT_CONFIRMATION
                APPT_LOCK_UNTIL = time.time() + APPT_LOCK_SECS
        except Exception as e:
            print(f"[APPT] error: {e}")
        time.sleep(5)

# -----------------------------------------------------------------------------
# Lanzar threads
# -----------------------------------------------------------------------------
threading.Thread(target=mcp_worker, daemon=True).start()
threading.Thread(target=auto_reminder_poller, daemon=True).start()
threading.Thread(target=appointment_poller, daemon=True).start()

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
            if porcupine.process(pcm_unpacked) >= 0:
                print("[DETECTED] Wake word")
                frames, dur = record_utterance_vad(prebuffer=pre_buffer_frames)
                save_audio_from_frames("wake_audio.wav", frames, SAMPLE_RATE)
                QUEUE.put(("wake_audio.wav", True))

        elif state == CONVERSATION_ACTIVE:
            if last_followup_sent_at and (time.time() - last_followup_sent_at) < FOLLOWUP_COOLDOWN_S:
                continue
            frames, dur = wait_for_speech_then_record_vad(timeout_s=FOLLOWUP_LISTEN_WINDOW_S)
            if frames:
                save_audio_from_frames("followup.wav", frames, SAMPLE_RATE)
                QUEUE.put(("followup.wav", True))
                last_activity_time = time.time()
                last_followup_sent_at = time.time()
            else:
                state = IDLE

        elif state == WAITING_FOR_CONFIRMATION:
            frames, dur = wait_for_speech_then_record_vad(timeout_s=12)
            if frames:
                tmpf = "confirm.wav"
                save_audio_from_frames(tmpf, frames, SAMPLE_RATE)
                text = stt_transcribe_wav(tmpf); os.remove(tmpf)
                intent = classify_confirmation_local(text)
                if intent == "yes":
                    speak("Perfecto. He registrado que tomaste tu medicamento. ¡Bien hecho!")
                elif intent == "no":
                    speak("De acuerdo. Te recordaré más tarde. Por favor, no lo olvides.")
                else:
                    speak("No te escuché bien. ¿La tomaste? Responde sí o no.")
                REMINDER_LOCK_UNTIL = time.time() + REMINDER_LOCK_SECS
                state = IDLE
            else:
                REMINDER_LOCK_UNTIL = time.time() + REMINDER_LOCK_SECS
                state = IDLE

        elif state == WAITING_FOR_APPT_CONFIRMATION:
            frames, dur = wait_for_speech_then_record_vad(timeout_s=12)
            if frames:
                tmpf = "appt_confirm.wav"
                save_audio_from_frames(tmpf, frames, SAMPLE_RATE)
                text = stt_transcribe_wav(tmpf); os.remove(tmpf)
                intent = classify_confirmation_local(text)
                if intent == "yes":
                    speak("Estupendo. He registrado que asistirás a tu cita.")
                elif intent == "no":
                    speak("Entendido. Avisaré más tarde para reprogramar si es necesario.")
                else:
                    speak("No te escuché bien. ¿Vas a asistir? Responde sí o no.")
                APPT_LOCK_UNTIL = time.time() + APPT_LOCK_SECS
                state = IDLE
            else:
                APPT_LOCK_UNTIL = time.time() + APPT_LOCK_SECS
                state = IDLE

except KeyboardInterrupt:
    print("\nShutting down...")
finally:
    try:
        stream.stop_stream(); stream.close()
    except Exception: pass
    pa.terminate()
    try: porcupine.delete()
    except Exception: pass