import os
import io
import time
import wave
import base64
import queue
import struct
import shutil
import threading
from collections import deque

import requests
import pyaudio
import pvporcupine

from pydub import AudioSegment
import tempfile
import statistics

# =============================================================================
# Configuración
# =============================================================================

ACCESS_KEY = "heQRVcJzahp/QdflX+KJRkOr6yvkclzaAKK6fY1NEKdYwtowZocbOg=="

# --- ENDPOINTS de tu API ---
BASE_API_URL      = "https://dreamincode-abgjgwgfckbqergq.eastus-01.azurewebsites.net"  # sin slash final
VOICE_MCP_URL     = f"{BASE_API_URL}/voice_mcp"
REMINDER_TTS_URL  = f"{BASE_API_URL}/reminder_tts"     # recordatorio + “¿ya te la tomaste?”
CONFIRM_URL       = f"{BASE_API_URL}/confirm_intake"   # clasifica sí/no/unsure y devuelve audio
TTS_URL           = f"{BASE_API_URL}/tts"              # (opcional) TTS libre

USER_ID = 3                 # id del adulto mayor
CHANNELS = 1
QUEUE = queue.Queue()

# VAD / conversación
MIN_SPEECH_MS = 500            # mínimo de voz acumulada para considerar “frase” válida
TRAILING_SILENCE_MS = 700      # silencio para cortar al final
MAX_UTTERANCE_S = 8            # tope duro por utterance
FOLLOWUP_LISTEN_WINDOW_S = 10  # ventana para esperar que el usuario empiece a hablar
FOLLOWUP_COOLDOWN_S = 0.8      # anti rebote tras enviar un followup

# Estados
IDLE = "IDLE"
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
# Rutas locales (wait tone + wakeword)
# -----------------------------------------------------------------------------
try:
    BASE_DIR = os.path.dirname(os.path.abspath(_file_))
except NameError:
    BASE_DIR = os.getcwd()

WAIT_AUDIO_PATH = os.path.join(BASE_DIR, "PrefabAudios", "waitResponse.wav")

# Cambia esta ruta por tu keyword .ppn (si no existe, se usa “porcupine” automáticamente)
WAKE_DIR = os.path.join(BASE_DIR, "Wakewords")
KEYWORD_PATH = os.path.join(WAKE_DIR, "Hey-Dream_en_raspberry-pi_v3_0_0.ppn")  # <-- ajusta a tu .ppn real

# =============================================================================
# Utilidades de audio
# =============================================================================

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

def save_audio_from_frames(filename, frames, sample_rate, sample_width=2, channels=1):
    with wave.open(filename, 'wb') as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(b''.join(frames))

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

def play_b64_audio(audio_b64: str, mime: str | None):
    try:
        audio_bytes = base64.b64decode(audio_b64)
        play_response_bytes(audio_bytes, mime or "audio/wav")
    except Exception as e:
        print(f"[AUDIO] base64 decode/play failed: {e}")

# =============================================================================
# Wake-word + entrada de micrófono
# =============================================================================

def _init_porcupine():
    """Intenta usar tu .ppn; si no existe, cae a 'porcupine'."""
    if os.path.exists(KEYWORD_PATH):
        print(f"[WAKE] usando keyword: {KEYWORD_PATH}")
        return pvporcupine.create(
            access_key=ACCESS_KEY,
            keyword_paths=[KEYWORD_PATH],
            sensitivities=[0.65]
        )
    else:
        print("[WAKE] keyword .ppn no encontrado; usando 'porcupine'")
        return pvporcupine.create(
            access_key=ACCESS_KEY,
            keywords=["porcupine"]
        )

porcupine = _init_porcupine()
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
    n = len(pcm_bytes) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack("<" + "h"*n, pcm_bytes[:n*2])
    acc = 0
    for s in samples:
        acc += s*s
    return (acc / n) ** 0.5

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
pre_buffer_frames = deque(maxlen=int(SAMPLE_RATE / FRAME_LEN * 1))  # ~1s

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

# =============================================================================
# Worker: /voice_mcp  (con “wait tone” una sola vez por petición)
# =============================================================================

def mcp_worker():
    global state, last_activity_time, last_followup_sent_at
    while True:
        file_to_upload, expect_followup = QUEUE.get()
        try:
            # dispara el audio de espera (una sola vez)
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

# =============================================================================
# Recordatorios con /reminder_tts (auto=true) + /confirm_intake
# =============================================================================

def get_tz_offset_min() -> int:
    import time as _t
    if _t.localtime().tm_isdst and _t.daylight:
        off_sec = -_t.altzone
    else:
        off_sec = -_t.timezone
    return int(off_sec / 60)

def reminder_scheduler():
    """
    Cada minuto:
      - POST /reminder_tts?mode=json {usuario_id, auto: true, tz_offset_min}
      - Si 200: reproduce recordatorio (con “¿ya te la tomaste?”) y escucha tu respuesta.
      - Envía audio a /confirm_intake y reproduce la confirmación.
    """
    global state
    tz_off = get_tz_offset_min()

    while True:
        try:
            r = requests.post(
                REMINDER_TTS_URL + "?mode=json",
                json={"usuario_id": USER_ID, "auto": True, "tz_offset_min": tz_off},
                timeout=20,
            )

            if r.status_code == 404:
                time.sleep(60)
                continue

            if r.status_code != 200:
                print(f"[REMINDER] HTTP {r.status_code}: {r.text[:160]}")
                time.sleep(60)
                continue

            payload = r.json()
            audio_b64 = payload.get("audio_base64")
            audio_mime = payload.get("audio_mime", "audio/wav")
            med_name  = payload.get("medicamento", "")
            med_hora  = payload.get("hora", "")

            if not audio_b64:
                print("[REMINDER] payload sin audio_base64")
                time.sleep(60)
                continue

            # Reproduce recordatorio y pasa a esperar confirmación
            play_b64_audio(audio_b64, audio_mime)
            state = WAITING_FOR_CONFIRMATION

            # Escucha respuesta del usuario
            frames, dur = wait_for_speech_then_record_vad(timeout_s=15)
            if not frames:
                state = IDLE
                time.sleep(60)
                continue

            # Enviar a confirm_intake
            tmp = "confirm.wav"
            save_audio_from_frames(tmp, frames, SAMPLE_RATE)
            try:
                with open(tmp, "rb") as f:
                    rr = requests.post(
                        CONFIRM_URL,
                        files={"audio": (tmp, f, "audio/wav")},
                        data={
                            "usuario_id": USER_ID,
                            "medicamento": med_name,
                            "hora": med_hora,
                            "return": "audio",
                        },
                        timeout=30,
                    )
                if rr.status_code == 200:
                    play_response_bytes(rr.content, rr.headers.get("Content-Type"))
                else:
                    print(f"[CONFIRM] HTTP {rr.status_code}: {rr.text[:160]}")
            finally:
                try:
                    os.remove(tmp)
                except Exception:
                    pass

            state = IDLE

        except Exception as e:
            print(f"[REMINDER] error: {e}")

        time.sleep(60)

# =============================================================================
# Lanzar threads y bucle principal
# =============================================================================

threading.Thread(target=mcp_worker, daemon=True).start()
threading.Thread(target=reminder_scheduler, daemon=True).start()  # activa recordatorios

print("Listening for wake word...")

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
            # anti rebote para followups
            if (time.time() - last_activity_time) > 10:
                state = IDLE
                continue

            # sólo si estás en diálogo libre con /voice_mcp
            if state == CONVERSATION_ACTIVE:
                frames, dur = wait_for_speech_then_record_vad(timeout_s=FOLLOWUP_LISTEN_WINDOW_S)
                if frames:
                    print(f"[REC] follow-up dur={dur:.2f}s frames={len(frames)}")
                    save_audio_from_frames("followup.wav", frames, SAMPLE_RATE)
                    print("[REC] archivo= followup.wav; subiendo…")
                    QUEUE.put(("followup.wav", True))
                    last_activity_time = time.time()
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
    try:
        pa.terminate()
    except Exception:
        pass
    try:
        porcupine.delete()
    except Exception:
        pass