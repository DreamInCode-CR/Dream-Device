import sounddevice as sd
import vosk
import queue
import json
import requests
import pyttsx3

q = queue.Queue()
model = vosk.Model("model")

def audio_callback(indata, frames, time, status):
    if status:
        print(status)
    q.put(bytes(indata))

def listen_and_transcribe():
    with sd.RawInputStream(samplerate=16000, blocksize=8000, dtype='int16',
                           channels=1, callback=audio_callback):
        print("Listening for speech...")
        rec = vosk.KaldiRecognizer(model, 16000)
        while True:
            data = q.get()
            if rec.AcceptWaveform(data):
                result = json.loads(rec.Result())
                return result.get("text", "")

def query_llm(text):
    url = "https://your-api.com/ask"
    response = requests.post(url, json={"query": text})
    if response.ok:
        return response.json().get("response", "")
    return "Sorry, I couldn’t reach the assistant."

def speak(text):
    engine = pyttsx3.init()
    engine.say(text)
    engine.runAndWait()

# Main loop
while True:
    transcript = listen_and_transcribe()
    if transcript:
        print("You said:", transcript)
        # response = query_llm(transcript)
        # print("Assistant:", response)
        speak(transcript)
