# iot_audio.py  (fixed for out-of-order/late chunks)

import os, json, time, threading, traceback, base64, wave
from pathlib import Path
from typing import Optional, Dict, Any, Callable

from dotenv import load_dotenv
from awscrt import io, mqtt
from awsiot import mqtt_connection_builder

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

SELF_TEST_PING = True
WILDCARD_SUB   = False

mqtt_connection: Optional[mqtt.Connection] = None

def _log(*args):
    print("[iot_edge]", *args, flush=True)

def _publish(event: Dict[str, Any]):
    assert mqtt_connection is not None, "MQTT connection not initialized"
    payload = json.dumps(event)
    mqtt_connection.publish(topic=TOPIC_EVENTS, payload=payload, qos=mqtt.QoS.AT_LEAST_ONCE)
    _log("TX", TOPIC_EVENTS, payload)

def publish_status(status: str, detail: Optional[Dict[str, Any]] = None):
    _publish({"type": "status", "status": status, "detail": detail or {}, "ts": int(time.time())})

def publish_error(msg: str, detail: Optional[Dict[str, Any]] = None, req_id: Optional[str] = None):
    _publish({"type": "error", "req_id": req_id, "message": msg, "detail": detail or {}, "ts": int(time.time())})

def publish_agent_result(req_id: Optional[str], movement: bool, verbal_output: str, motion_plan: str):
    _publish({
        "type": "agent_result",
        "req_id": req_id,
        "movement": movement,
        "verbal_output": verbal_output,
        "motion_plan": motion_plan,
        "ts": int(time.time())
    })

# ----------------------------------------------------------------------
# Existing text commands (unchanged)
# ----------------------------------------------------------------------
def handle_run_agent(cmd: Dict[str, Any]):
    req_id = cmd.get("req_id")
    text   = cmd.get("text", "")
    if not text.strip():
        publish_error("run_agent missing 'text'", req_id=req_id)
        return
    try:
        raw = call_agent(text)
        movement, say, motion = parse_agent_response(raw)

        if say and say != "N/A":
            text_to_speech(say)

        publish_agent_result(req_id, movement, say, motion)
    except Exception as e:
        traceback.print_exc()
        publish_error(str(e), req_id=req_id)

def handle_say(cmd: Dict[str, Any]):
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

# ----------------------------------------------------------------------
# FIXED: audio streaming commands (seq-buffer + finalize grace)
# ----------------------------------------------------------------------
AUDIO_SESSIONS: Dict[str, Dict[str, Any]] = {}
AUDIO_DIR = Path("received_audio")
AUDIO_DIR.mkdir(exist_ok=True)

FINALIZE_GRACE_MS = 400  # wait this long after audio.end for late chunks

def handle_audio_start(cmd: Dict[str, Any]):
    req_id = cmd.get("req_id")
    if not req_id:
        publish_error("audio.start missing req_id")
        return
    AUDIO_SESSIONS[req_id] = {
        "sr": int(cmd.get("sample_rate", 24000)),
        "channels": int(cmd.get("channels", 1)),
        "width": int(cmd.get("sample_width", 2)),
        "frame_ms": int(cmd.get("frame_ms", 30)),
        # store by seq so order doesn't matter
        "chunks": {},              # seq -> pcm bytes
        "lock": threading.Lock(),
        "expected_chunks": None,
        "ended": False,
        "t0": time.time(),
        "last_seq_seen": -1,
    }
    publish_status("audio_started", {"req_id": req_id, "sr": AUDIO_SESSIONS[req_id]["sr"]})
    _log("audio.start", req_id)

def handle_audio_chunk(cmd: Dict[str, Any]):
    req_id = cmd.get("req_id")
    sess = AUDIO_SESSIONS.get(req_id)
    if sess is None:
        # late chunk after finalize — ignore quietly
        _log("audio.chunk for unknown req_id", req_id, "(late?)")
        return

    try:
        seq = int(cmd.get("seq", -1))
        b64 = cmd.get("pcm16_b64", "")
        if seq < 0 or not b64:
            return
        pcm = base64.b64decode(b64)

        with sess["lock"]:
            if seq != sess["last_seq_seen"] + 1:
                _log(f"audio.chunk out of order: got {seq}, expected {sess['last_seq_seen']+1}")
            sess["last_seq_seen"] = max(sess["last_seq_seen"], seq)
            sess["chunks"][seq] = pcm  # overwrite duplicates safely

    except Exception as e:
        publish_error(f"audio.chunk decode failed: {e}", req_id=req_id)

def _finalize_audio_session(req_id: str):
    sess = AUDIO_SESSIONS.get(req_id)
    if sess is None:
        return

    # grace window for late chunks
    time.sleep(FINALIZE_GRACE_MS / 1000.0)

    # snapshot chunks under lock
    with sess["lock"]:
        chunks_dict = dict(sess["chunks"])
        sr, ch, sw = sess["sr"], sess["channels"], sess["width"]
        expected = sess.get("expected_chunks")

    if expected is not None:
        missing = [i for i in range(expected) if i not in chunks_dict]
        if missing:
            _log(f"finalize: missing {len(missing)} of {expected} chunks (ok over MQTT)")

    # assemble in seq order
    pcm = b"".join(chunks_dict[i] for i in sorted(chunks_dict.keys()))

    wav_path = AUDIO_DIR / f"{req_id}.wav"
    try:
        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(ch)
            wf.setsampwidth(sw)
            wf.setframerate(sr)
            wf.writeframes(pcm)

        dur_s = len(pcm) / float(sr * ch * sw)
        publish_status("audio_saved", {
            "req_id": req_id,
            "path": str(wav_path),
            "seconds": round(dur_s, 3),
            "bytes": len(pcm),
        })
        _log("audio.end saved", wav_path, f"{dur_s:.2f}s")

    except Exception as e:
        traceback.print_exc()
        publish_error(f"audio.end save failed: {e}", req_id=req_id)

    # remove session after finalize
    AUDIO_SESSIONS.pop(req_id, None)

def handle_audio_end(cmd: Dict[str, Any]):
    req_id = cmd.get("req_id")
    sess = AUDIO_SESSIONS.get(req_id)
    if sess is None:
        publish_error("audio.end for unknown req_id", req_id=req_id)
        return

    sess["ended"] = True
    try:
        sess["expected_chunks"] = int(cmd.get("num_chunks")) if cmd.get("num_chunks") is not None else None
    except Exception:
        sess["expected_chunks"] = None

    # finalize in background so late chunks can still arrive
    threading.Thread(target=_finalize_audio_session, args=(req_id,), daemon=True).start()

COMMAND_TABLE: Dict[str, Callable[[Dict[str, Any]], None]] = {
    "run_agent": handle_run_agent,
    "say":       handle_say,
    "ping":      handle_ping,
    "audio.start": handle_audio_start,
    "audio.chunk": handle_audio_chunk,
    "audio.end":   handle_audio_end,
}

def on_message(topic, payload, dup, qos, retain, **kwargs):
    try:
        body = payload.decode()
        _log("RX", topic, f"{body[:120]}{'...' if len(body)>120 else ''}")
        cmd = json.loads(body)
        cmd_type = cmd.get("type")
        handler = COMMAND_TABLE.get(cmd_type)
        if not handler:
            publish_error(f"unknown command '{cmd_type}'", detail={"payload": cmd})
            return
        handler(cmd)
    except Exception as e:
        traceback.print_exc()
        try:
            publish_error(str(e))
        except Exception:
            _log("Error handling incoming message:", repr(e))

def connect_and_listen():
    global mqtt_connection

    event_loop_group = io.EventLoopGroup(1)
    host_resolver    = io.DefaultHostResolver(event_loop_group)
    client_bootstrap = io.ClientBootstrap(event_loop_group, host_resolver)

    lwt = json.dumps({
        "type": "status",
        "status": "offline",
        "client_id": CLIENT_ID,
        "ts": int(time.time())
    }).encode()

    mqtt_connection = mqtt_connection_builder.mtls_from_path(
        endpoint=ENDPOINT,
        cert_filepath=CERT,
        pri_key_filepath=KEY,
        ca_filepath=ROOT_CA,
        client_bootstrap=client_bootstrap,
        client_id=CLIENT_ID,
        clean_session=True,
        keep_alive_secs=30,
        will=mqtt.Will(
            topic=TOPIC_EVENTS,
            qos=mqtt.QoS.AT_LEAST_ONCE,
            payload=lwt,
            retain=False,
        ),
    )

    _log(f"Connecting to {ENDPOINT} as {CLIENT_ID} …")
    mqtt_connection.connect().result()
    _log("Connected.")

    sub_topic = "#" if WILDCARD_SUB else TOPIC_CMDS
    sub_future, _ = mqtt_connection.subscribe(
        topic=sub_topic,
        qos=mqtt.QoS.AT_LEAST_ONCE,
        callback=on_message
    )
    sub_future.result()
    _log(f"Subscribed to {sub_topic}")

    publish_status("online", {"client_id": CLIENT_ID})

    if SELF_TEST_PING:
        mqtt_connection.publish(
            topic=TOPIC_CMDS,
            payload=json.dumps({"type": "ping", "req_id": "self-test"}),
            qos=mqtt.QoS.AT_LEAST_ONCE,
        )
        _log("Published self-test ping")

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
