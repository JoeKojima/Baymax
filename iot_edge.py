"""
IoT Edge bridge: connects the local voice assistant stack to AWS IoT Core via MQTT.
"""
import os
import json
import time
import threading
import traceback
from typing import Optional, Dict, Any, Callable

from dotenv import load_dotenv
from awscrt import io, mqtt
from awsiot import mqtt_connection_builder

# Local modules
from agent import call_agent, parse_agent_response
from tts import text_to_speech

load_dotenv()

ENDPOINT   = os.environ["AWS_IOT_ENDPOINT"]
CLIENT_ID  = os.environ.get("AWS_IOT_CLIENT_ID", "voice-assistant")
CERT       = os.environ["AWS_IOT_CERT"]
KEY        = os.environ["AWS_IOT_KEY"]
ROOT_CA    = os.environ["AWS_IOT_ROOT_CA"]

TOPIC_CMDS   = os.environ.get("AWS_IOT_TOPIC_CMDS", "voice/commands")
TOPIC_EVENTS = os.environ.get("AWS_IOT_TOPIC_EVENTS", "voice/events")

# Global MQTT connection
mqtt_connection: Optional[mqtt.Connection] = None

def _log(*args):
    print("[iot_edge]", *args, flush=True)

def _publish(event: Dict[str, Any]):
    """Internal helper: publish a device event to the cloud."""
    assert mqtt_connection is not None
    payload = json.dumps(event)
    mqtt_connection.publish(
        topic=TOPIC_EVENTS,
        payload=payload,
        qos=mqtt.QoS.AT_LEAST_ONCE
    )
    _log("TX", TOPIC_EVENTS, payload)

def publish_transcript(text: str, req_id: Optional[str] = None):
    """Call this from voice_assistant after you transcribe user audio."""
    evt = {
        "type": "transcript",
        "req_id": req_id,
        "text": text,
        "ts": int(time.time())
    }
    _publish(evt)

def publish_agent_result(req_id: Optional[str], movement: bool, verbal_output: str, motion_plan: str):
    """Call this from voice_assistant after agent returns."""
    evt = {
        "type": "agent_result",
        "req_id": req_id,
        "movement": movement,
        "verbal_output": verbal_output,
        "motion_plan": motion_plan,
        "ts": int(time.time())
    }
    _publish(evt)

def publish_status(status: str, detail: Optional[Dict[str, Any]] = None):
    evt = {"type": "status", "status": status, "detail": detail or {}, "ts": int(time.time())}
    _publish(evt)

def publish_error(msg: str, detail: Optional[Dict[str, Any]] = None, req_id: Optional[str] = None):
    evt = {"type": "error", "req_id": req_id, "message": msg, "detail": detail or {}, "ts": int(time.time())}
    _publish(evt)

# -------- Command handlers (cloud -> device) --------

def handle_run_agent(cmd: Dict[str, Any]):
    """Run the local LLM agent on provided text and publish result."""
    req_id = cmd.get("req_id")
    text   = cmd.get("text", "")
    if not text.strip():
        publish_error("run_agent missing 'text'", req_id=req_id)
        return
    try:
        raw = call_agent(text)
        movement, say, motion = parse_agent_response(raw)
        # Speak if applicable
        if say and say != "N/A":
            text_to_speech(say)
        publish_agent_result(req_id, movement, say, motion)
    except Exception as e:
        traceback.print_exc()
        publish_error(str(e), req_id=req_id)

def handle_say(cmd: Dict[str, Any]):
    """Speak text via local TTS."""
    req_id = cmd.get("req_id")
    text   = cmd.get("text", "")
    if not text.strip():
        publish_error("say missing 'text'", req_id=req_id)
        return
    try:
        text_to_speech(text)
        publish_status("spoke", {"req_id": req_id, "length": len(text)})
    except Exception as e:
        traceback.print_exc()
        publish_error(str(e), req_id=req_id)

def handle_ping(cmd: Dict[str, Any]):
    req_id = cmd.get("req_id")
    publish_status("pong", {"req_id": req_id})

COMMAND_TABLE: Dict[str, Callable[[Dict[str, Any]], None]] = {
    "run_agent": handle_run_agent,
    "say": handle_say,
    "ping": handle_ping,
}

def on_message(topic, payload, dup, qos, retain, **kwargs):
    """Incoming command dispatcher."""
    try:
        body = payload.decode()
        _log("RX", topic, body)
        cmd = json.loads(body)
        cmd_type = cmd.get("type")
        handler = COMMAND_TABLE.get(cmd_type)
        if not handler:
            publish_error(f"unknown command '{cmd_type}'", detail={"payload": cmd})
            return
        handler(cmd)
    except Exception as e:
        traceback.print_exc()
        publish_error(str(e))

def connect_and_listen():
    """Connect to AWS IoT and subscribe to commands."""
    global mqtt_connection

    event_loop_group = io.EventLoopGroup(1)
    host_resolver    = io.DefaultHostResolver(event_loop_group)
    client_bootstrap = io.ClientBootstrap(event_loop_group, host_resolver)

    # Last-will so cloud sees offline status
    lwt = json.dumps({"type":"status","status":"offline","client_id":CLIENT_ID,"ts":int(time.time())}).encode()

    mqtt_connection = mqtt_connection_builder.mtls_from_path(
        endpoint=ENDPOINT,
        cert_filepath=CERT,
        pri_key_filepath=KEY,
        ca_filepath=ROOT_CA,
        client_bootstrap=client_bootstrap,
        client_id=CLIENT_ID,
        clean_session=True,
        keep_alive_secs=30,
        will=mqtt.Will(topic=TOPIC_EVENTS, qos=mqtt.QoS.AT_LEAST_ONCE, payload=lwt, retain=False),
    )

    _log(f"Connecting to {ENDPOINT} as {CLIENT_ID} …")
    mqtt_connection.connect().result()
    _log("Connected.")

    mqtt_connection.subscribe(
        topic=TOPIC_CMDS,
        qos=mqtt.QoS.AT_LEAST_ONCE,
        callback=on_message
    )
    _log(f"Subscribed to {TOPIC_CMDS}")

    publish_status("online", {"client_id": CLIENT_ID})

def start_in_background():
    """Optional: start the MQTT loop in a thread so your main app can run normally."""
    t = threading.Thread(target=connect_and_listen, daemon=True)
    t.start()
    return t

if __name__ == "__main__":
    connect_and_listen()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if mqtt_connection:
            mqtt_connection.disconnect().result()
            _log("Disconnected.")
