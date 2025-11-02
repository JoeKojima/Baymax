import json
import time
import uuid
import os
from awscrt import io, mqtt
from awsiot import mqtt_connection_builder
from dotenv import load_dotenv

# NEW: pull in your audio pipeline
from audio_processing import record_until_silence, transcribe_audio

load_dotenv()

ENDPOINT   = os.environ["AWS_IOT_ENDPOINT"]
CLIENT_ID  = os.environ.get("AWS_IOT_CLIENT_ID", "cloud-tester")
CERT       = os.environ["AWS_IOT_CERT"]
KEY        = os.environ["AWS_IOT_KEY"]
ROOT_CA    = os.environ["AWS_IOT_ROOT_CA"]

TOPIC_CMDS   = os.environ.get("AWS_IOT_TOPIC_CMDS", "voice/commands")
TOPIC_EVENTS = os.environ.get("AWS_IOT_TOPIC_EVENTS", "voice/events")

# inbox stores agent_result replies keyed by req_id
inbox = {}

def on_event(topic, payload, **kwargs):
    """
    Handle messages coming back from the device.
    We watch for 'agent_result' messages and stash them in inbox[req_id].
    We also just print every event for debugging / visibility.
    """
    msg = payload.decode()
    print("[cloud] <-- event:", topic, msg)

    try:
        data = json.loads(msg)
    except json.JSONDecodeError:
        return

    if data.get("type") == "agent_result":
        req_id = data.get("req_id")
        if req_id:
            inbox[req_id] = data

def connect_mqtt():
    """
    Connect to AWS IoT Core over mTLS and subscribe to the events topic.
    Returns an active mqtt_connection.
    """
    event_loop_group = io.EventLoopGroup(1)
    host_resolver    = io.DefaultHostResolver(event_loop_group)
    client_bootstrap = io.ClientBootstrap(event_loop_group, host_resolver)

    mqtt_connection = mqtt_connection_builder.mtls_from_path(
        endpoint=ENDPOINT,
        cert_filepath=CERT,
        pri_key_filepath=KEY,
        ca_filepath=ROOT_CA,
        client_bootstrap=client_bootstrap,
        client_id=CLIENT_ID + "-cloud",
        clean_session=True,
        keep_alive_secs=30
    )

    print("[cloud] Connecting...")
    mqtt_connection.connect().result()
    print("[cloud] Connected.")

    # subscribe once and keep it for the whole session
    mqtt_connection.subscribe(
        topic=TOPIC_EVENTS,
        qos=mqtt.QoS.AT_LEAST_ONCE,
        callback=on_event
    )

    return mqtt_connection

def send_run_agent_and_wait(mqtt_connection, user_text: str, timeout_s: float = 30.0):
    """
    Send a 'run_agent' command with the given user_text,
    then block until we get a matching agent_result or timeout.
    Returns (verbal_output, motion_plan, movement) or (None, None, None) on timeout.
    """
    # unique correlation ID
    req_id = "req-" + str(uuid.uuid4())

    cmd_run_agent = {
        "type": "run_agent",
        "req_id": req_id,
        "text": user_text
    }

    print(f"[cloud] --> sending run_agent for req_id={req_id}")
    mqtt_connection.publish(
        topic=TOPIC_CMDS,
        payload=json.dumps(cmd_run_agent),
        qos=mqtt.QoS.AT_LEAST_ONCE
    )

    print("[cloud] waiting for device response (and for device to speak on its side)...")

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if req_id in inbox:
            data = inbox.pop(req_id)
            verbal_output = data.get("verbal_output", "")
            motion_plan   = data.get("motion_plan", "")
            movement      = data.get("movement", False)
            return verbal_output, motion_plan, movement
        time.sleep(0.2)

    print("[cloud] timeout: no response from device within", timeout_s, "seconds")
    return None, None, None

def main():
    mqtt_connection = connect_mqtt()

    print()
    print("===================================================")
    print("Voice conversation mode")
    print("Say something, pause, let it listen.")
    print("Ctrl+C to end.")
    print("===================================================")
    print()

    try:
        while True:
            # 1. capture your voice locally (mic on THIS machine)
            audio_file = record_until_silence()
            print("[cloud] captured audio, transcribing...")

            # 2. transcribe to text using Whisper/OpenAI
            user_text = transcribe_audio(audio_file)
            user_text = user_text.strip()
            print(f"You (transcribed): {user_text}")

            if not user_text:
                print("[cloud] (empty / silence, ignoring)")
                continue

            # 3. send text down to the device as a run_agent command
            verbal_output, motion_plan, movement = send_run_agent_and_wait(
                mqtt_connection,
                user_text,
                timeout_s=30.0
            )

            # 4. show what the device said (it should have already spoken out loud via TTS on its side)
            if verbal_output is not None:
                print()
                print("Device replied (spoken on edge):", verbal_output)
                print("Motion plan:", motion_plan)
                print("Movement?:", "YES" if movement else "no")
                print()

    except KeyboardInterrupt:
        print("\n[cloud] stopping voice session...")

    finally:
        print("[cloud] disconnecting MQTT...")
        try:
            mqtt_connection.disconnect().result()
        except Exception as e:
            print("[cloud] disconnect warning:", e)
        print("[cloud] done.")

if __name__ == "__main__":
    main()
