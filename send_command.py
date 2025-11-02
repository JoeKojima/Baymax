import json
import time
import uuid
from awscrt import io, mqtt
from awsiot import mqtt_connection_builder
import os
from dotenv import load_dotenv

load_dotenv()

ENDPOINT   = os.environ["AWS_IOT_ENDPOINT"]
CLIENT_ID  = os.environ.get("AWS_IOT_CLIENT_ID", "cloud-tester")
CERT       = os.environ["AWS_IOT_CERT"]
KEY        = os.environ["AWS_IOT_KEY"]
ROOT_CA    = os.environ["AWS_IOT_ROOT_CA"]

TOPIC_CMDS   = os.environ.get("AWS_IOT_TOPIC_CMDS", "voice/commands")
TOPIC_EVENTS = os.environ.get("AWS_IOT_TOPIC_EVENTS", "voice/events")

# shared dict for latest response
inbox = {}

def on_event(topic, payload, **kwargs):
    """Handle messages coming back from the device."""
    msg = payload.decode()
    print("[cloud] <-- event:", msg)
    try:
        data = json.loads(msg)
    except json.JSONDecodeError:
        return

    # We only really care about agent_result for convo
    if data.get("type") == "agent_result":
        req_id = data.get("req_id")
        if req_id:
            inbox[req_id] = data

# ---------- Connect ----------
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

# subscribe to events once, so we get agent_result / errors / status
mqtt_connection.subscribe(
    topic=TOPIC_EVENTS,
    qos=mqtt.QoS.AT_LEAST_ONCE,
    callback=on_event
)

print("You are now talking to the device.")
print("Type a message and hit Enter. Type /quit to exit.\n")

try:
    while True:
        user_text = input("You: ").strip()
        if user_text.lower() in ["/quit", "/exit"]:
            break
        if not user_text:
            continue

        # make a unique request id for this turn
        req_id = "req-" + str(uuid.uuid4())

        # build the command we send down
        cmd_run_agent = {
            "type": "run_agent",
            "req_id": req_id,
            "text": user_text
        }

        print(f"[cloud] --> sending run_agent {req_id!r} ...")
        mqtt_connection.publish(
            topic=TOPIC_CMDS,
            payload=json.dumps(cmd_run_agent),
            qos=mqtt.QoS.AT_LEAST_ONCE
        )

        # wait for reply from device with matching req_id
        print("[cloud] waiting for device response...")
        deadline = time.time() + 30  # 30s timeout so we don't hang forever
        while time.time() < deadline:
            if req_id in inbox:
                result = inbox.pop(req_id)
                # Show what the device said (this is also what it should have spoken via TTS)
                verbal_output = result.get("verbal_output", "")
                motion_plan   = result.get("motion_plan", "")
                movement      = result.get("movement", False)

                print(f"\n[device voice]: {verbal_output}")
                print(f"[motion_plan]: {motion_plan}")
                print(f"[movement?]:   {'YES' if movement else 'no'}\n")
                break
            time.sleep(0.2)
        else:
            print("[cloud] (timeout waiting for response)\n")

finally:
    print("[cloud] disconnecting...")
    try:
        mqtt_connection.disconnect().result()
    except Exception as e:
        print("[cloud] disconnect error (already closed ok):", e)
    print("[cloud] done.")
