"""Text-to-speech module using pyttsx3."""
import pyttsx3


def text_to_speech(text: str, rate: int = 150, volume: float = 1.0):
    engine = pyttsx3.init()
    engine.setProperty('rate', rate)
    engine.setProperty('volume', volume)
    engine.say(text)
    engine.runAndWait()
