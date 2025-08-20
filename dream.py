import os
import io
import time
import wave
import queue
import struct
import threading
import shutil
import tempfile
import statistics
from collections import deque

import pvporcupine
import pyaudio
import requests
from pydub import AudioSegment

# =============================================================================
# Configuration
# =============================================================================

ACCESS_KEY = "heQRVcJzahp/QdflX+KJRkOr6yvkclzaAKK6fY1NEKdYwtowZocbOg=="

# API endpoints
API_BASE_URL       = "https://dreamincode-abgjgwgfckbqergq.eastus-01.azurewebsites.net"
VOICE_MCP_URL      = f"{API_BASE_URL}/voice_mcp"
REMINDER_TTS_URL   = f"{API_BASE_URL}/reminder_tts"   # manual/auto reminder
TTS_URL            = f"{API_BASE_URL}/tts"            # free-form TTS
MEDS_ALL_URL       = f"{API_BASE_URL}/meds/all"       # list all meds (JSON)
STT_URL            = f"{API_BASE_URL}/stt"            # STT for yes/no confirmation
MEDS_DUE_URL       = f"{API_BASE_URL}/meds/due"       # exact due calculation

USER_ID  = 3           # target user id
CHANNELS = 1
RATE     = 16000       # target for normalization / saving WAV (some helpers use 16k)
QUEUE    = queue.Queue()

# VAD / conversation parameters
MIN_SPEECH_MS             = 500
TRAILING_SILENCE_MS       = 700
MAX_UTTERANCE_S           = 8
FOLLOWUP_LISTEN_WINDOW_S  = 10
FOLLOWUP_COOLDOWN_S       = 0.8

# States
IDLE                      = "IDLE"
WAITING_FOR_SPEECH        = "WAITING_FOR_SPEECH"
CONVERSATION_ACTIVE       = "CONVERSATION_ACTIVE"
WAITING_FOR_CONFIRMATION  = "WAITING_FOR_CONFIRMATION"
state                     = IDLE
last_activity_time        = time.time()
last_followup_sent_at     = 0.0

# Timing (for metrics/diagnostics)
last_rec_started_at       = 0.0

# Reminder lock: avoid repeating the same reminder too frequently
REMINDER_LOCK_UNTIL       = 0.0   # epoch seconds; if now < lock, skip reminders
REMINDER_LOCK_SECS        = 120.0 # suppress for 2 minutes

# =============================================================================
# FFmpeg configuration (for pydub)
# =============================================================================
os.environ.setdefault("FFMPEG_BINARY", "ffmpeg")
try:
    if os.path.exists("/usr/bin/ffmpeg"):
        AudioSegment.converter = "/usr/bin/ffmpeg"
except Exception:
    # Non-fatal if ffmpeg path cannot be set here
    pass

# =============================================================================
# Local wait audio (played once per request)
# =============================================================================
try:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
except Exception:
    BASE_DIR = os.getcwd()

WAIT_AUDIO_PATH = os.path.join(BASE_DIR, "PrefabAudios", "waitResponse.wav")


def play_wav(path: str):
    """Play a WAV file using 'aplay' quietly."""
    os.system(f"aplay -q '{path}' >/dev/null 2>&1")


def _normalize_wait_wav(src_path: str) -> str:
    """Normalize the wait audio to 16 kHz, mono, 16-bit PCM for consistent playback."""
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
    print(f"[WAIT] File not found: {WAIT_AUDIO_PATH}")

# =============================================================================
# Local WAV utilities
# =============================================================================
def save_audio_from_frames(filename: str, frames: list[bytes], sample_rate: int,
                           sample_width: int = 2, channels: int = 1):
    """Save audio frames into a WAV file."""
    with wave.open(filename, 'wb') as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(b''.join(frames))

# =============================================================================
# Robust playback utilities (handle content-type/bytes sniffing)
# =============================================================================
def _fmt_from_header(ct: str | None) -> str | None:
    """Infer audio format from a Content-Type header."""
    if not ct:
        return None
    c = ct.lower()
    if "wav" in c or "wave" in c:
        return "wav"
    if "mpeg" in c or "mp3" in c:
        return "mp3"
    return None


def _sniff_fmt(b: bytes) -> str | None:
    """Infer audio format by inspecting raw bytes."""
    if len(b) >= 12 and b[:4] == b"RIFF" and b[8:12] == b"WAVE":
        return "wav"
    if len(b) >= 2 and (b[:3] == b"ID3" or (b[0] == 0xFF and (b[1] & 0xE0) == 0xE0)):
        return "mp3"
    return None


def _have_mpg123() -> bool:
    """Return True if 'mpg123' is available on this system."""
    return shutil.which("mpg123") is not None


def play_response_bytes(resp_bytes: bytes, content_type: str | None):
    """Play audio from response bytes (supports WAV/MP3; falls back to decode via pydub)."""
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
                # Decode with pydub if mpg123 is not present
                audio = AudioSegment.from_file(io.BytesIO(resp_bytes), format="mp3")
                audio = audio.set_frame_rate(16000).set_channels(1).set_sample_width(2)
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                    audio.export(tmp.name, format="wav", parameters=["-acodec", "pcm_s16le"])
                    path = tmp.name
                play_wav(path)
                os.remove(path)
        else:
            # Fallback: let pydub detect and transcode to WAV for playback
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

# =============================================================================
# Timezone offset utility (local vs UTC, handles DST)
# =============================================================================
def get_tz_offset_min() -> int:
    """Return the local timezone offset from UTC in minutes."""
    import datetime as _dt
    now = _dt.datetime.now()
    utc = _dt.datetime.utcnow()
    return int(round((now - utc).total_seconds() / 60.0))

# =============================================================================
# Porcupine + PyAudio initialization and noise calibration
# =============================================================================
WAKE_DIR = os.path.join(BASE_DIR, "Wakewords")
KEYWORD_PATH = os.path.join(WAKE_DIR, "Hey-Dream_en_raspberry-pi_v3_0_0.ppn")

porcupine = pvporcupine.create(
    access_key=ACCESS_KEY,
    keyword_paths=[KEYWORD_PATH],
    sensitivities=[0.65],
)

SAMPLE_RATE = porcupine.sample_rate        # typically 16000
FRAME_LEN   = porcupine.frame_length       # typically 512
FRAME_MS    = int(1000 * FRAME_LEN / SAMPLE_RATE)

pa = pyaudio.PyAudio()
stream = pa.open(
    rate=SAMPLE_RATE,
    channels=CHANNELS,
    format=pyaudio.paInt16,
    input=True,
    frames_per_buffer=FRAME_LEN,
)


def _rms_int16(pcm_bytes: bytes) -> float:
    """Compute RMS of 16-bit PCM bytes."""
    if not pcm_bytes:
        return 0.0
    count = len(pcm_bytes) // 2
    if count == 0:
        return 0.0
    samples = struct.unpack("<" + "h" * count, pcm_bytes[:count * 2])
    acc = 0
    for s in samples:
        acc += s * s
    return (acc / count) ** 0.5


def calibrate_noise(frames: int = 50) -> float:
    """Calibrate noise level and set an energy threshold for VAD."""
    vals = []
    for _ in range(frames):
        b = stream.read(FRAME_LEN, exception_on_overflow=False)
        vals.append(_rms_int16(b))
    med = statistics.median(vals)
    thr = max(300.0, med * 3.0)
    print(f"[VAD] noise median={med:.1f} -> threshold={thr:.1f}")
    return thr


ENERGY_THRESHOLD = calibrate_noise()
pre_buffer_frames = deque(maxlen=int(SAMPLE_RATE / FRAME_LEN * 1))  # ~1 second prebuffer

# =============================================================================
# VAD-controlled recording
# =============================================================================
def record_utterance_vad(prebuffer: deque[bytes] | None = None) -> tuple[list[bytes], float]:
    """Record an utterance based on simple energy VAD with trailing silence."""
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


def wait_for_speech_then_record_vad(timeout_s: float = FOLLOWUP_LISTEN_WINDOW_S) -> tuple[list[bytes], float]:
    """Wait for speech up to timeout, then record an utterance using VAD."""
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
# STT + local confirmation classification (YES/NO)
# =============================================================================
def stt_transcribe_wav(path: str) -> str:
    """Send WAV to STT endpoint and return the transcription (Spanish)."""
    try:
        with open(path, "rb") as f:
            r = requests.post(
                STT_URL,
                files={'audio': (os.path.basename(path), f, 'audio/wav')},
                data={"usuario_id": USER_ID, "lang": "es"},
                timeout=30,
            )
        if r.status_code == 200:
            data = r.json()
            return (data.get("transcripcion") or "").strip()
        else:
            print(f"[STT] HTTP {r.status_code}: {r.text[:160]}")
            return ""
    except Exception as e:
        print(f"[STT] error: {e}")
        return ""


def classify_confirmation_local(text: str) -> str:
    """
    Classify 'yes' / 'no' / 'unsure' from Spanish free-form text.
    This is a simple keyword heuristic.
    """
    t = (text or "").strip().lower()
    if not t:
        return "unsure"

    yes_words = [
        "sí", "si", "ya", "claro", "por supuesto", "listo", "hecho",
        "me la tomé", "me la tome", "ya la tomé", "ya la tome",
        "ya lo hice", "la tomé", "la tome",
    ]
    no_words = [
        "no", "todavía no", "aún no", "aun no", "después", "luego",
        "más tarde", "mas tarde", "no la tomé", "no la tome",
        "no lo hice",
    ]

    for w in yes_words:
        if w in t:
            return "yes"
    for w in no_words:
        if w in t:
            return "no"
    return "unsure"

# =============================================================================
# Worker: send audio to backend and play response
# =============================================================================
def mcp_worker():
    """Background worker that uploads recorded audio to the MCP and plays responses."""
    global state, last_activity_time, last_followup_sent_at
    while True:
        file_to_upload, expect_followup = QUEUE.get()
        try:
            # Play local "wait" audio concurrently
            if WAIT_AUDIO_PLAY_PATH and os.path.exists(WAIT_AUDIO_PLAY_PATH):
                threading.Thread(target=play_wav, args=(WAIT_AUDIO_PLAY_PATH,), daemon=True).start()

            with open(file_to_upload, 'rb') as f:
                t0 = time.time()
                response = requests.post(
                    VOICE_MCP_URL,
                    files={'audio': (file_to_upload, f, 'audio/wav')},
                    data={"usuario_id": USER_ID, "lang": "es"},
                    timeout=60,
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
# Extra utilities (optional)
# =============================================================================
def speak(text: str):
    """TTS helper that sends text to TTS endpoint and plays the audio."""
    try:
        r = requests.post(TTS_URL, json={"texto": text}, timeout=30)
        if r.status_code == 200:
            play_response_bytes(r.content, r.headers.get("Content-Type"))
        else:
            print(f"[SPEAK] HTTP {r.status_code}: {r.text[:160]}")
    except Exception as e:
        print(f"[SPEAK] error: {e}")


def fetch_medications():
    """Fetch and print medication list count (diagnostic)."""
    try:
        r = requests.get(MEDS_ALL_URL, params={"usuario_id": USER_ID}, timeout=10)
        print(f"[MEDS] GET /meds/all -> {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            print(f"[MEDS] count={data.get('count')}")
        else:
            print(f"[MEDS] payload: {r.text[:160]}")
    except Exception as e:
        print(f"[MEDS] error: {e}")

# =============================================================================
# Exact poller: use /meds/due (window_min=0) then /reminder_tts manually
# =============================================================================
def auto_reminder_poller():
    """
    Poll for medications exactly due now (window_min=0), and if found,
    request manual reminder TTS and move to WAITING_FOR_CONFIRMATION.
    """
    global state, REMINDER_LOCK_UNTIL
    while True:
        try:
            now = time.time()
            if now < REMINDER_LOCK_UNTIL or state == WAITING_FOR_CONFIRMATION:
                time.sleep(1.0)
                continue

            tz = get_tz_offset_min()

            # 1) Ask if there is something due exactly now (no window)
            g = requests.get(
                MEDS_DUE_URL,
                params={"usuario_id": USER_ID, "window_min": 0, "tz_offset_min": tz},
                timeout=10,
            )
            if g.status_code != 200:
                time.sleep(5)
                continue

            items = (g.json() or {}).get("items", [])
            if not items:
                time.sleep(5)
                continue

            # 2) Take the first item and request manual reminder TTS (no window)
            it = items[0]
            payload = {
                "usuario_id": USER_ID,
                "auto": False,
                "medicamento": it.get("medicamento") or it.get("NombreMedicamento") or "",
                "dosis": it.get("dosis") or it.get("Dosis") or "",
                "hora": it.get("hora") or it.get("HoraToma") or "",
            }
            if not payload["medicamento"] or not payload["hora"]:
                time.sleep(5)
                continue

            r = requests.post(REMINDER_TTS_URL, json=payload, timeout=20)
            if r.status_code == 200:
                print(f"[AUTO] ok tz={tz} ct={r.headers.get('Content-Type')}")
                play_response_bytes(r.content, r.headers.get("Content-Type"))
                state = WAITING_FOR_CONFIRMATION
                REMINDER_LOCK_UNTIL = time.time() + REMINDER_LOCK_SECS
            else:
                print(f"[AUTO] HTTP {r.status_code}: {r.text[:160]}")

        except Exception as e:
            print(f"[AUTO] error: {e}")

        time.sleep(5)

# =============================================================================
# Launch background threads
# =============================================================================
threading.Thread(target=mcp_worker, daemon=True).start()
threading.Thread(target=auto_reminder_poller, daemon=True).start()
threading.Thread(target=fetch_medications, daemon=True).start()

print("Listening for wake word...")

# =============================================================================
# Main loop
# =============================================================================
try:
    while True:
        try:
            pcm = stream.read(FRAME_LEN, exception_on_overflow=False)
        except Exception as e:
            print(f"[Stream Error] {e}")
            continue

        pre_buffer_frames.append(pcm)
        pcm_unpacked = struct.unpack_from("h" * (len(pcm) // 2), pcm)

        if state == IDLE:
            keyword_index = porcupine.process(pcm_unpacked)
            if keyword_index >= 0:
                print("[DETECTED] Wake word")
                frames, dur = record_utterance_vad(prebuffer=pre_buffer_frames)
                print(f"[REC] wake dur={dur:.2f}s frames={len(frames)}")
                save_audio_from_frames("wake_audio.wav", frames, SAMPLE_RATE)
                print("[REC] file=wake_audio.wav; uploading…")
                QUEUE.put(("wake_audio.wav", True))

        elif state == CONVERSATION_ACTIVE:
            if last_followup_sent_at and (time.time() - last_followup_sent_at) < FOLLOWUP_COOLDOWN_S:
                continue

            frames, dur = wait_for_speech_then_record_vad(timeout_s=FOLLOWUP_LISTEN_WINDOW_S)
            if frames:
                print(f"[REC] follow-up dur={dur:.2f}s frames={len(frames)}")
                save_audio_from_frames("followup.wav", frames, SAMPLE_RATE)
                print("[REC] file=followup.wav; uploading…")
                QUEUE.put(("followup.wav", True))
                last_activity_time = time.time()
                last_followup_sent_at = time.time()
            else:
                state = IDLE

        elif state == WAITING_FOR_CONFIRMATION:
            print("[CONFIRM] Waiting for confirmation (yes/no)…")
            frames, dur = wait_for_speech_then_record_vad(timeout_s=12)
            if frames:
                print(f"[CONFIRM] capture dur={dur:.2f}s frames={len(frames)}")
                tmpf = "confirm.wav"
                save_audio_from_frames(tmpf, frames, SAMPLE_RATE)
                text = stt_transcribe_wav(tmpf)
                print(f"[CONFIRM] transcript='{text}'")
                try:
                    os.remove(tmpf)
                except Exception:
                    pass

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
                print("[CONFIRM] No response detected; returning to IDLE")
                state = IDLE

except KeyboardInterrupt:
    print("\nShutting down...")

finally:
    # Graceful teardown
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
