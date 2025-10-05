# Voice Assistant for Elderly Care

A Python-based voice assistant that listens continuously, processes speech using OpenAI Whisper, generates responses with GPT-4, and speaks back using ElevenLabs text-to-speech.

## Features

- **Continuous listening**: Always-on voice detection
- **Automatic silence detection**: Stops recording after 2 seconds of silence
- **Speech-to-Text**: OpenAI Whisper API
- **LLM Processing**: GPT-4 with scratchpad functionality
- **Text-to-Speech**: ElevenLabs voice synthesis
- **Scratchpad**: Tracks conversation history in JSON format

## Setup

1. **Install system dependencies**:
   ```bash
   brew install portaudio
   ```

2. **Create virtual environment**:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

3. **Install Python packages**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure API keys**:
   ```bash
   cp .env.example .env
   ```
   Then edit `.env` and add your actual API keys:
   - `OPENAI_API_KEY`: Get from https://platform.openai.com/api-keys
   - `ELEVENLABS_API_KEY`: Get from https://elevenlabs.io/

## Usage

Run the voice assistant:
```bash
source venv/bin/activate
python voice_assistant.py
```

The assistant will:
1. Listen for your voice
2. Detect when you start speaking
3. Record until 2 seconds of silence
4. Transcribe your speech
5. Process with GPT-4
6. Speak the response back to you

Press `Ctrl+C` to stop.

## Output Format

The LLM provides three outputs:
- **Movement**: Whether physical movement is required
- **Audio output**: Verbal response (spoken via ElevenLabs)
- **Motion plan**: Movement instructions (currently N/A)

All interactions are logged to `scratchpad.json`.
