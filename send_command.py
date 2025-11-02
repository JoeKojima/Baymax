import json
import time
import uuid
import os
from awscrt import io, mqtt
from awsiot import mqtt_connection_builder
from dotenv import load_dotenv
from openai import OpenAI

from audio_processing import MicrophoneStream

load_dotenv()

ENDPOINT   = os.environ["AWS_IOT_ENDPOINT"]
CLIENT_ID  = os.environ.get("AWS_IOT_CLIENT_ID", "cloud-tester")
CERT       = os.environ["AWS_IOT_CERT"]
KEY        = os.environ["AWS_IOT_KEY"]
ROOT_CA    = os.environ["AWS_IOT_ROOT_CA"]

TOPIC_CMDS   = os.environ.get("AWS_IOT_TOPIC_CMDS", "voice/commands")
TOPIC_EVENTS = os.environ.get("AWS_IOT_TOPIC_EVENTS", "voice/events")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

inbox = {}

def on_event(topic, payload, **kwargs):
    msg = payload.decode()
    print("[cloud] <-- event:", msg)
    try:
        data = json.loads(msg)
    except json.JSONDecodeError:
        return
    if data.get("type") == "agent_result":
        inbox[data["req_id"]] = data

def connect_mqtt():
    event_loop_group = io.EventLoopGroup(1)
    host_resolver    = io.DefaultHostResolver(event_loop_group)
    client_bootstrap = io.ClientBootstrap(event_loop_group, host_resolver)
    mqtt_connection = mqtt_connection_builder.mtls_from_path(
        endpoint=ENDPOINT,
        cert_filepath=CERT,
        pri_key_filepath=KEY,
        ca_filepath=ROOT_CA,
        client_bootstrap=client_bootstrap,
        client_id=CLIENT_ID + "-voice",
        clean_session=True,
        keep_alive_secs=30,
    )
    print("[cloud] Connecting...")
    mqtt_connection.connect().result()
    print("[cloud] Connected.")
    mqtt_connection.subscribe(
        topic=TOPIC_EVENTS,
        qos=mqtt.QoS.AT_LEAST_ONCE,
        callback=on_event
    )
    return mqtt_connection

def record_and_transcribe(duration_s=4):
    """Record a few seconds of audio and transcribe with Whisper."""
    mic = MicrophoneStream()
    print("🎤 Speak now...")
    frames = []
    for _ in range(int((24000 / mic.stream._frames_per_buffer) * duration_s)):
        frames.append(mic.__next__())
    mic.close()

    import io, wave
    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(b''.join(frames))
    wav_buffer.seek(0)
    wav_buffer.name = "input.wav"

    print("Transcribing...")
    transcript = client.audio.transcriptions.create(
        model="whisper-1",
        file=wav_buffer
    )
    text = transcript.text.strip()
    print(f"You (transcribed): {text}")
    return text

def send_run_agent_and_wait(mqtt_connection, user_text, timeout_s=30):
    req_id = "req-" + str(uuid.uuid4())
    cmd = {"type": "run_agent", "req_id": req_id, "text": user_text}
    mqtt_connection.publish(
        topic=TOPIC_CMDS,
        payload=json.dumps(cmd),
        qos=mqtt.QoS.AT_LEAST_ONCE
    )
    print("[cloud] Sent command, waiting for reply...")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if req_id in inbox:
            data = inbox.pop(req_id)
            return (
                data.get("verbal_output"),
                data.get("motion_plan"),
                data.get("movement")
            )
        time.sleep(0.2)
    print("[cloud] Timeout waiting for reply")
    return None, None, None

def main():
    mqtt_connection = connect_mqtt()
    print("🎙️ Voice conversation active. Ctrl+C to stop.")
    try:
        while True:
            user_text = record_and_transcribe(duration_s=4)
            if not user_text:
                continue
            verbal_output, motion_plan, movement = send_run_agent_and_wait(
                mqtt_connection, user_text
            )
            if verbal_output:
                print(f"Device (spoken): {verbal_output}")
    except KeyboardInterrupt:
        pass
    finally:
        mqtt_connection.disconnect().result()
        print("Disconnected.")

if __name__ == "__main__":
    main()
