import pyaudio
from pocketsphinx import LiveSpeech

print("🔊 Listening for wake word... (say 'assistant')")

speech = LiveSpeech(
    verbose=False,
    sampling_rate=16000,
    buffer_size=2048,
    no_search=False,
    full_utt=False,
    keyphrase='assistant',  # your chosen wake word
    kws_threshold=1e-20     # lower = more sensitive
)

for phrase in speech:
    print("🟢 Wake word detected!")
    # Now you could record audio and send to your API