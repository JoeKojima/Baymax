# send_mic_mqtt_chunks.py
import os, json, time, uuid, base64
import numpy as np
from dotenv import load_dotenv
from awscrt import io, mqtt
from awsiot import mqtt_connection_builder

from audio_processing import MicrophoneStream, SAMPLE_RATE, CHANNELS, SAMPLE_WIDTH, FRAME_DURATION_MS

load_dotenv()

ENDPOINT   = os.environ["AWS_IOT_ENDPOINT"]
CLIENT_ID  = os.environ.get("AWS_IOT_CLIENT_ID", "cloud-tester")
CERT       = os.environ["AWS_IOT_CERT"]
KEY        = os.environ["AWS_IOT_KEY"]
ROOT_CA    = os.environ["AWS_IOT_ROOT_CA"]

TOPIC_CMDS   = os.environ.get("AWS_IOT_TOPIC_CMDS", "voice/commands")
TOPIC_EVENTS = os.environ.get("AWS_IOT_TOPIC_EVENTS", "voice/events")

# ---- very simple VAD (energy threshold) ----
RMS_THRESHOLD = 600        # tweak if it triggers too easily / not enough
SILENCE_MS    = 700        # end utterance after this much silence

def connect_mqtt():
    event_loop_group = io.EventLoopGroup(1)
    host_resolver    = io.DefaultHostResolver(event_loop_group)
    client_bootstrap = io.ClientBootstrap(event_loop_group, host_resolver)
    conn = mqtt_connection_builder.mtls_from_path(
        endpoint=ENDPOINT,
        cert_filepath=CERT,
        pri_key_filepath=KEY,
        ca_filepath=ROOT_CA,
        client_bootstrap=client_bootstrap,
        client_id=CLIENT_ID + "-micstream",
        clean_session=True,
        keep_alive_secs=30,
    )
    print("[cloud] Connecting...")
    conn.connect().result()
    print("[cloud] Connected.")
    return conn

def publish(conn, msg: dict):
    conn.publish(
        topic=TOPIC_CMDS,
        payload=json.dumps(msg),
        qos=mqtt.QoS.AT_LEAST_ONCE
    )

def rms_of_pcm16(frame_bytes: bytes) -> float:
    arr = np.frombuffer(frame_bytes, dtype=np.int16)
    if arr.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(arr.astype(np.float32) ** 2)))

def main():
    conn = connect_mqtt()
    mic = MicrophoneStream()

    print("🎙️ Speak; Ctrl+C to stop.")
    req_id = None
    seq = 0
    silence_accum_ms = 0

    try:
        for frame in mic:
            r = rms_of_pcm16(frame)

            speaking = r >= RMS_THRESHOLD

            # start a new audio session on first speech
            if speaking and req_id is None:
                req_id = "aud-" + str(uuid.uuid4())
                seq = 0
                silence_accum_ms = 0
                publish(conn, {
                    "type": "audio.start",
                    "req_id": req_id,
                    "sample_rate": SAMPLE_RATE,
                    "channels": CHANNELS,
                    "sample_width": SAMPLE_WIDTH,  # bytes per sample (2 for PCM16)
                    "frame_ms": FRAME_DURATION_MS,
                    "ts": int(time.time())
                })
                print(f"[cloud] audio.start {req_id}")

            if req_id is not None:
                # while session open, send every frame
                publish(conn, {
                    "type": "audio.chunk",
                    "req_id": req_id,
                    "seq": seq,
                    "pcm16_b64": base64.b64encode(frame).decode("ascii"),
                    "ts": int(time.time())
                })
                seq += 1

                if speaking:
                    silence_accum_ms = 0
                else:
                    silence_accum_ms += FRAME_DURATION_MS

                # end session after sustained silence
                if silence_accum_ms >= SILENCE_MS:
                    publish(conn, {
                        "type": "audio.end",
                        "req_id": req_id,
                        "num_chunks": seq,
                        "ts": int(time.time())
                    })
                    print(f"[cloud] audio.end {req_id} ({seq} chunks)")
                    req_id = None
                    seq = 0
                    silence_accum_ms = 0

    except KeyboardInterrupt:
        if req_id is not None:
            publish(conn, {
                "type": "audio.end",
                "req_id": req_id,
                "num_chunks": seq,
                "ts": int(time.time())
            })
            print(f"[cloud] audio.end {req_id} ({seq} chunks)")

    finally:
        mic.close()
        conn.disconnect().result()
        print("[cloud] Disconnected.")

if __name__ == "__main__":
    main()
