import json
import time
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

def on_event(topic, payload, **kwargs):
    print("[cloud side] got event:", topic, payload.decode())

# --- connect ---
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

print("[cloud side] Connecting...")
mqtt_connection.connect().result()
print("[cloud side] Connected.")

# 1) subscribe once so we see ALL replies (pong, agent_result, errors, etc.)
mqtt_connection.subscribe(
    topic=TOPIC_EVENTS,
    qos=mqtt.QoS.AT_LEAST_ONCE,
    callback=on_event
)

# 2) send ping
cmd_ping = {
    "type": "ping",
    "req_id": "test-123"
}
print("[cloud side] sending ping...")
mqtt_connection.publish(
    topic=TOPIC_CMDS,
    payload=json.dumps(cmd_ping),
    qos=mqtt.QoS.AT_LEAST_ONCE
)

# short wait just to see pong
time.sleep(2)

# 3) send run_agent
cmd_run_agent = {
    "type": "run_agent",
    "req_id": "job-789",
    "text": "Help and grab the doctor."
}
print("[cloud side] sending run_agent...")
mqtt_connection.publish(
    topic=TOPIC_CMDS,
    payload=json.dumps(cmd_run_agent),
    qos=mqtt.QoS.AT_LEAST_ONCE
)

# 4) wait long enough for the model to answer AND for TTS to run
print("[cloud side] waiting for response...")
time.sleep(20)

# 5) disconnect once at the end
mqtt_connection.disconnect().result()
print("[cloud side] Done.")
