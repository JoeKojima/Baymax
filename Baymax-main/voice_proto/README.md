# Ember voice ID prototype (Week 3, step 1)

Offline test: can a pretrained model (SpeechBrain ECAPA-TDNN) tell our voices apart,
and what thresholds should Ember use? Nothing here touches the robot code yet.

## Setup (Windows Git Bash)
    python -m venv .venv
    source .venv/Scripts/activate
    pip install -r requirements.txt

## Get clips (pick one or both)
    python record_clips.py --name stella          # your own voice, 8 clips
    python record_clips.py --name roommate        # anyone else nearby
    python prep_kcl.py --data PATH/TO/MDVR-KCL --out kcl_clips --max-speakers 10

## Run
    python identify.py --clips clips
    python identify.py --clips kcl_clips --out kcl_results.csv

## Read the output
- accuracy: % of test clips given the right name (closed set, no "unknown" yet)
- min true / max wrong: if min true > max wrong, a threshold between them names
  everyone correctly and never names the wrong person
- embedding time: rough per-clip speed on your laptop (the LattePanda will be slower)
