from pocketsphinx import LiveSpeech

# Create a LiveSpeech object listening for the word "hello"
speech = LiveSpeech(lm=False, keyphrase='hello', kws_threshold=1e-20)

print("Listening for the wake word 'hello'...")

# Infinite loop listening for wake word
for phrase in speech:
    print("Wake word detected:", phrase)
