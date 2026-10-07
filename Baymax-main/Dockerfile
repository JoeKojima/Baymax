# Ember robot software: the AI core (realtime_gemini_10.py) and the local
# dashboard (baymax_app.py), run as two containers from this one image.
# See deploy/docker/README.md for how it is built and run on a robot.
#
# Everything robot-specific or private lives outside the image:
#   /etc/ember/device.toml     per-robot settings        (mounted read-only)
#   /etc/ember/secrets.env     API keys, email password  (passed as env vars)
#   /var/lib/ember -> /data    logs, memory, accounts    (mounted read-write)
FROM python:3.13-slim-trixie

# Runtime libraries the Python packages expect from the OS:
#   libportaudio2           sounddevice
#   pipewire-alsa           ALSA "pipewire" device, so audio goes to the host's PipeWire
#   pulseaudio-utils        pactl, used to pin the USB speaker as the default sink
#   libgl1, libglib2.0-0    OpenCV
#   libegl1, libgles2       MediaPipe
#   libsndfile1             soundfile (voice analysis)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libportaudio2 pipewire-alsa pulseaudio-utils \
        libgl1 libglib2.0-0 libegl1 libgles2 libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock

# Models the code loads from next to itself, baked in so a robot needs no
# downloads at first boot: the ONNX sentence embedder (semantic memory) and
# the MediaPipe pose model (fall detection, face presence).
COPY convert_model.py .
RUN python convert_model.py && rm -rf /root/.cache/huggingface
RUN python -c "import urllib.request; urllib.request.urlretrieve(\
'https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/latest/pose_landmarker_full.task', \
'pose_landmarker_full.task')"

# The container runs as the host user that owns the PipeWire session and
# /var/lib/ember (uid 1000 on the LattePanda; compose can override it).
RUN useradd --uid 1000 --create-home ember

# NLTK data for the voice analyzer, which calls nltk.download() on start. The
# folder belongs to the container user so that call finds it up to date
# instead of re-downloading on every container start.
ENV NLTK_DATA=/usr/local/share/nltk_data
RUN python -c "import nltk; [nltk.download(r, download_dir='$NLTK_DATA', quiet=True) \
for r in ('punkt_tab', 'averaged_perceptron_tagger')]" \
    && chown -R ember "$NLTK_DATA"

COPY . .

USER ember
ENV BAYMAX_DATA_DIR=/data \
    PYTHONUNBUFFERED=1

# The two services pick their command in deploy/docker/compose.yaml.
CMD ["python", "realtime_gemini_10.py"]
