"""Main voice assistant application."""
from typing import TypedDict, List, Dict
from audio_processing import record_until_silence, transcribe_audio
from agent import call_agent, parse_agent_response
from tts import text_to_speech
from scratchpad import load_scratchpad, save_scratchpad, create_entry, initialize_scratchpad


class State(TypedDict):
    scratchpad: List[Dict[str, str]]
    text: str
    img_path: str | None


def run_voice_assistant():
    """Main loop for voice assistant."""
    # Initialize scratchpad
    initialize_scratchpad()

    state: State = {"scratchpad": load_scratchpad(), "text": "", "img_path": None}
    print("Voice assistant started. Listening continuously...")

    while True:
        try:
            # Record audio until silence
            audio_file = record_until_silence()

            # Transcribe to text
            user_text = transcribe_audio(audio_file)
            print(f"You: {user_text}")

            if not user_text.strip():
                continue

            # Process with LLM agent
            raw = call_agent(user_text)
            movement, say, motion = parse_agent_response(raw)

            print(f"Movement: {'Yes' if movement else 'No'}")
            print(f"Audio output: {say or 'N/A'}")
            print(f"Motion plan: {motion or 'N/A'}")

            # Speak response
            if say and say != "N/A":
                text_to_speech(say)

            # Update scratchpad
            entry = create_entry(user_text, movement, say, motion)
            state["scratchpad"].append(entry)
            save_scratchpad(state["scratchpad"])
            print(f"[Scratchpad entries: {len(state['scratchpad'])}]\n")

        except KeyboardInterrupt:
            print("\nShutting down...")
            break
        except Exception as e:
            print(f"Error: {e}")
            continue


if __name__ == "__main__":
    run_voice_assistant()
