"""
Voice Biomarker Analysis Module
Analyzes speech recordings for acoustic and linguistic biomarkers
that may indicate neurodegenerative decline.
Adapted from voice_analysis_prototype/speech_biomarker_analysis.py.
"""
import os
import json
import time
import wave
import glob
import numpy as np
from typing import Dict, List, Optional, Any
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_PATH = os.path.join(SCRIPT_DIR, "voice_analysis_results.json")
ALERTS_PATH = os.path.join(SCRIPT_DIR, "voice_alerts.json")
BASELINE_MIN_SESSIONS = 5
DEVIATION_THRESHOLD = 1.5

CLINICAL_PATTERNS = {
    "MCI": {
        "name": "Mild Cognitive Impairment",
        "min_correlated": 3,
        "indicators": [
            {"metric": "temporal.speech_rate_wpm", "direction": "low"},
            {"metric": "temporal.mean_pause_duration_s", "direction": "high"},
            {"metric": "lexical.type_token_ratio", "direction": "low"},
            {"metric": "semantic.idea_density", "direction": "low"},
            {"metric": "lexical.lexical_density", "direction": "low"},
            {"metric": "syntactic.noun_to_pronoun_ratio", "direction": "low"},
        ],
    },
    "Alzheimers": {
        "name": "Alzheimer's Disease Pattern",
        "min_correlated": 3,
        "indicators": [
            {"metric": "semantic.topic_coherence", "direction": "low"},
            {"metric": "semantic.idea_density", "direction": "low"},
            {"metric": "lexical.type_token_ratio", "direction": "low"},
            {"metric": "syntactic.mlu_words", "direction": "low"},
            {"metric": "temporal.hesitation_ratio", "direction": "high"},
            {"metric": "syntactic.noun_to_pronoun_ratio", "direction": "low"},
        ],
    },
    "Parkinsons": {
        "name": "Parkinson's Disease Pattern",
        "min_correlated": 3,
        "indicators": [
            {"metric": "prosodic.f0_coefficient_variation", "direction": "low"},
            {"metric": "vocal_quality.jitter_local", "direction": "high"},
            {"metric": "vocal_quality.shimmer_local", "direction": "high"},
            {"metric": "vocal_quality.hnr_db", "direction": "low"},
            {"metric": "temporal.speech_rate_wpm", "direction": "low"},
        ],
    },
    "Depression": {
        "name": "Depression Pattern",
        "min_correlated": 3,
        "indicators": [
            {"metric": "temporal.speech_rate_wpm", "direction": "low"},
            {"metric": "prosodic.f0_coefficient_variation", "direction": "low"},
            {"metric": "temporal.mean_pause_duration_s", "direction": "high"},
        ],
    },
}

ANALYSIS_CATEGORIES = [
    "vocal_quality", "prosodic", "temporal", "lexical", "syntactic", "semantic"
]


def _load_json(path):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


class VoiceAnalyzer:
    def __init__(self):
        self._nlp = None
        self._nltk_ready = False

    def _ensure_nlp(self):
        if self._nlp is not None:
            return
        import spacy
        try:
            self._nlp = spacy.load("en_core_web_sm")
        except OSError:
            print("[VOICE] Downloading spaCy model en_core_web_sm...")
            os.system("python3 -m spacy download en_core_web_sm")
            self._nlp = spacy.load("en_core_web_sm")

        if not self._nltk_ready:
            import nltk
            for resource in ["punkt_tab", "averaged_perceptron_tagger"]:
                try:
                    nltk.download(resource, quiet=True)
                except Exception:
                    pass
            self._nltk_ready = True

    def analyze_session(self, wav_path: str, transcript: str) -> Dict[str, Any]:
        import librosa
        y, sr = librosa.load(wav_path, sr=None)
        duration = librosa.get_duration(y=y, sr=sr)

        session_id = time.strftime("%Y%m%d_%H%M%S")
        print(f"[VOICE] Analyzing session {session_id} ({duration:.0f}s audio, {len(transcript.split())} words)")

        print("[VOICE] [1/6] Vocal quality (jitter, shimmer, HNR)...")
        vocal_quality = self._analyze_vocal_quality(wav_path)

        print("[VOICE] [2/6] Prosodic features (F0, pitch variation)...")
        prosodic = self._analyze_prosodic(wav_path)

        print("[VOICE] [3/6] Temporal features (speech rate, pauses)...")
        temporal = self._analyze_temporal(wav_path, transcript)

        print("[VOICE] [4/6] Lexical features (TTR, density)...")
        lexical = self._analyze_lexical(transcript)

        print("[VOICE] [5/6] Syntactic features (MLU, complexity)...")
        syntactic = self._analyze_syntactic(transcript)

        print("[VOICE] [6/6] Semantic features (idea density, coherence)...")
        semantic = self._analyze_semantic(transcript)

        return {
            "session_id": session_id,
            "timestamp": datetime.now().isoformat(),
            "duration_s": round(duration, 1),
            "word_count": len(transcript.split()),
            "vocal_quality": vocal_quality,
            "prosodic": prosodic,
            "temporal": temporal,
            "lexical": lexical,
            "syntactic": syntactic,
            "semantic": semantic,
        }

    def _analyze_vocal_quality(self, wav_path: str) -> Dict[str, Optional[float]]:
        try:
            import parselmouth
            from parselmouth.praat import call

            sound = parselmouth.Sound(wav_path)
            pitch = call(sound, "To Pitch", 0.0, 75, 600)
            point_process = call(sound, "To PointProcess (periodic, cc)", 75, 600)

            jitter = call(point_process, "Get jitter (local)", 0, 0, 0.0001, 0.02, 1.3)
            shimmer = call([sound, point_process], "Get shimmer (local)", 0, 0,
                           0.0001, 0.02, 1.3, 1.6)
            harmonicity = call(sound, "To Harmonicity (cc)", 0.01, 75, 0.1, 1.0)
            hnr = call(harmonicity, "Get mean", 0, 0)

            return {
                "jitter_local": float(jitter),
                "shimmer_local": float(shimmer),
                "hnr_db": float(hnr),
            }
        except Exception as e:
            print(f"[VOICE] Vocal quality analysis error: {e}")
            return {"jitter_local": None, "shimmer_local": None, "hnr_db": None}

    def _analyze_prosodic(self, wav_path: str) -> Dict[str, Optional[float]]:
        try:
            import parselmouth
            from parselmouth.praat import call

            sound = parselmouth.Sound(wav_path)
            pitch = call(sound, "To Pitch", 0.0, 75, 600)

            mean_f0 = call(pitch, "Get mean", 0, 0, "Hertz")
            std_f0 = call(pitch, "Get standard deviation", 0, 0, "Hertz")
            min_f0 = call(pitch, "Get minimum", 0, 0, "Hertz", "Parabolic")
            max_f0 = call(pitch, "Get maximum", 0, 0, "Hertz", "Parabolic")
            f0_cv = (std_f0 / mean_f0) if mean_f0 > 0 else 0

            return {
                "mean_f0_hz": float(mean_f0),
                "std_f0_hz": float(std_f0),
                "f0_range_hz": float(max_f0 - min_f0),
                "f0_coefficient_variation": float(f0_cv),
            }
        except Exception as e:
            print(f"[VOICE] Prosodic analysis error: {e}")
            return {
                "mean_f0_hz": None, "std_f0_hz": None,
                "f0_range_hz": None, "f0_coefficient_variation": None,
            }

    def _analyze_temporal(self, wav_path: str, transcript: str) -> Dict[str, Optional[float]]:
        try:
            import librosa

            y, sr = librosa.load(wav_path, sr=None)
            duration = librosa.get_duration(y=y, sr=sr)
            words = transcript.split()
            word_count = len(words)

            speech_rate = (word_count / duration) * 60 if duration > 0 else 0

            frame_length = 2048
            hop_length = 512
            energy = librosa.feature.rms(y=y, frame_length=frame_length,
                                         hop_length=hop_length)[0]
            threshold = np.percentile(energy, 20)
            silent_frames = energy < threshold
            frame_times = librosa.frames_to_time(np.arange(len(energy)),
                                                  sr=sr, hop_length=hop_length)

            pauses = []
            in_pause = False
            pause_start = 0.0
            for i, is_silent in enumerate(silent_frames):
                if is_silent and not in_pause:
                    in_pause = True
                    pause_start = frame_times[i]
                elif not is_silent and in_pause:
                    in_pause = False
                    pd = frame_times[i] - pause_start
                    if pd > 0.2:
                        pauses.append(pd)

            num_pauses = len(pauses)
            mean_pause_duration = float(np.mean(pauses)) if pauses else 0.0
            total_pause_time = sum(pauses) if pauses else 0.0
            phonation_time = duration - total_pause_time
            articulation_rate = (word_count / phonation_time) * 60 if phonation_time > 0 else 0
            hesitation_ratio = num_pauses / word_count if word_count > 0 else 0

            return {
                "speech_rate_wpm": float(speech_rate),
                "articulation_rate_wpm": float(articulation_rate),
                "num_pauses": int(num_pauses),
                "mean_pause_duration_s": mean_pause_duration,
                "total_pause_time_s": float(total_pause_time),
                "phonation_time_s": float(phonation_time),
                "pause_to_speech_ratio": float(total_pause_time / duration) if duration > 0 else 0,
                "hesitation_ratio": float(hesitation_ratio),
            }
        except Exception as e:
            print(f"[VOICE] Temporal analysis error: {e}")
            return {
                "speech_rate_wpm": None, "articulation_rate_wpm": None,
                "num_pauses": None, "mean_pause_duration_s": None,
                "total_pause_time_s": None, "phonation_time_s": None,
                "pause_to_speech_ratio": None, "hesitation_ratio": None,
            }

    def _analyze_lexical(self, transcript: str) -> Dict[str, Optional[float]]:
        try:
            self._ensure_nlp()
            from nltk.tokenize import word_tokenize

            tokens = word_tokenize(transcript.lower())
            words = [w for w in tokens if w.isalnum()]
            if not words:
                return {"word_count": 0, "unique_words": 0, "type_token_ratio": None,
                        "mattr": None, "lexical_density": None, "avg_word_length": None}

            types = set(words)
            ttr = len(types) / len(words)

            window_size = min(50, len(words))
            mattr_values = []
            for i in range(len(words) - window_size + 1):
                window = words[i:i + window_size]
                mattr_values.append(len(set(window)) / len(window))
            mattr = float(np.mean(mattr_values)) if mattr_values else ttr

            doc = self._nlp(transcript)
            content_words = [t for t in doc if t.pos_ in ["NOUN", "VERB", "ADJ", "ADV"]]
            lexical_density = len(content_words) / len(doc) if len(doc) > 0 else 0

            return {
                "word_count": len(words),
                "unique_words": len(types),
                "type_token_ratio": float(ttr),
                "mattr": float(mattr),
                "lexical_density": float(lexical_density),
                "avg_word_length": float(np.mean([len(w) for w in words])),
            }
        except Exception as e:
            print(f"[VOICE] Lexical analysis error: {e}")
            return {"word_count": None, "type_token_ratio": None,
                    "lexical_density": None, "mattr": None,
                    "unique_words": None, "avg_word_length": None}

    def _analyze_syntactic(self, transcript: str) -> Dict[str, Optional[float]]:
        try:
            self._ensure_nlp()
            doc = self._nlp(transcript)
            sentences = list(doc.sents)
            if not sentences:
                return {"mlu_words": None, "noun_to_pronoun_ratio": None,
                        "avg_syntactic_depth": None}

            words_per_sentence = [len([t for t in sent if not t.is_punct]) for sent in sentences]
            mlu = float(np.mean(words_per_sentence)) if words_per_sentence else 0

            nouns = [t for t in doc if t.pos_ == "NOUN"]
            pronouns = [t for t in doc if t.pos_ == "PRON"]
            total_words = len([t for t in doc if not t.is_punct])
            noun_to_pronoun = len(nouns) / len(pronouns) if len(pronouns) > 0 else 0

            def tree_depth(token):
                children = list(token.children)
                if not children:
                    return 1
                return 1 + max(tree_depth(c) for c in children)

            depths = [tree_depth(sent.root) for sent in doc.sents]
            avg_depth = float(np.mean(depths)) if depths else 0

            subordinating = [t for t in doc if t.dep_ in ["mark", "advcl", "relcl"]]
            clauses_per_sent = len(subordinating) / len(sentences) if sentences else 0

            return {
                "mlu_words": mlu,
                "num_sentences": len(sentences),
                "noun_count": len(nouns),
                "pronoun_count": len(pronouns),
                "noun_ratio": float(len(nouns) / total_words) if total_words > 0 else 0,
                "pronoun_ratio": float(len(pronouns) / total_words) if total_words > 0 else 0,
                "noun_to_pronoun_ratio": float(noun_to_pronoun),
                "avg_syntactic_depth": avg_depth,
                "clauses_per_sentence": float(clauses_per_sent),
            }
        except Exception as e:
            print(f"[VOICE] Syntactic analysis error: {e}")
            return {"mlu_words": None, "noun_to_pronoun_ratio": None,
                    "avg_syntactic_depth": None, "num_sentences": None,
                    "noun_count": None, "pronoun_count": None,
                    "noun_ratio": None, "pronoun_ratio": None,
                    "clauses_per_sentence": None}

    def _analyze_semantic(self, transcript: str) -> Dict[str, Optional[float]]:
        try:
            self._ensure_nlp()
            doc = self._nlp(transcript)

            verbs = [t for t in doc if t.pos_ == "VERB"]
            total_words = len([t for t in doc if not t.is_punct and not t.is_space])
            idea_density = len(verbs) / total_words if total_words > 0 else 0

            lemmas = [t.lemma_.lower() for t in doc
                      if not t.is_stop and not t.is_punct and t.is_alpha]
            unique_lemmas = len(set(lemmas))
            total_lemmas = len(lemmas)
            lemma_diversity = unique_lemmas / total_lemmas if total_lemmas > 0 else 0

            sentences = list(doc.sents)
            topic_coherence = 0.0
            if len(sentences) > 1:
                overlaps = []
                for i in range(len(sentences) - 1):
                    s1 = set(t.lemma_.lower() for t in sentences[i]
                             if not t.is_stop and t.is_alpha)
                    s2 = set(t.lemma_.lower() for t in sentences[i + 1]
                             if not t.is_stop and t.is_alpha)
                    if s1 and s2:
                        overlaps.append(len(s1 & s2) / len(s1 | s2))
                topic_coherence = float(np.mean(overlaps)) if overlaps else 0.0

            entities = [ent.text for ent in doc.ents]

            return {
                "idea_density": float(idea_density),
                "semantic_diversity": float(lemma_diversity),
                "unique_lemmas": unique_lemmas,
                "total_lemmas": total_lemmas,
                "topic_coherence": topic_coherence,
                "entity_count": len(entities),
                "unique_entities": len(set(entities)),
            }
        except Exception as e:
            print(f"[VOICE] Semantic analysis error: {e}")
            return {"idea_density": None, "semantic_diversity": None,
                    "topic_coherence": None, "unique_lemmas": None,
                    "total_lemmas": None, "entity_count": None,
                    "unique_entities": None}

    # ─── Baseline & Alert System ────────────────────────────────────────────

    def save_session_results(self, results: Dict) -> Dict[str, Any]:
        sessions = _load_json(RESULTS_PATH)
        sessions.append(results)
        _save_json(RESULTS_PATH, sessions)

        summary = {
            "session_count": len(sessions),
            "baseline_ready": len(sessions) >= BASELINE_MIN_SESSIONS,
            "deviations": [],
            "alerts": [],
        }

        if len(sessions) >= BASELINE_MIN_SESSIONS:
            prior = sessions[:-1]
            baseline = self._compute_baseline(prior)
            deviations = self._check_deviations(results, baseline)
            alerts = self._check_clinical_patterns(deviations)

            summary["deviations"] = deviations
            summary["alerts"] = alerts

            if alerts:
                existing_alerts = _load_json(ALERTS_PATH)
                existing_alerts.extend(alerts)
                _save_json(ALERTS_PATH, existing_alerts)

        return summary

    def _compute_baseline(self, sessions: List[Dict]) -> Dict[str, Dict]:
        baseline = {}
        for category in ANALYSIS_CATEGORIES:
            for session in sessions:
                cat_data = session.get(category)
                if not cat_data:
                    continue
                for key, value in cat_data.items():
                    if not isinstance(value, (int, float)) or value is None:
                        continue
                    metric_key = f"{category}.{key}"
                    if metric_key not in baseline:
                        baseline[metric_key] = []
                    baseline[metric_key].append(float(value))

        result = {}
        for metric_key, values in baseline.items():
            if len(values) >= 3:
                result[metric_key] = {
                    "mean": float(np.mean(values)),
                    "std": float(np.std(values)),
                    "n": len(values),
                }
        return result

    def _check_deviations(self, session: Dict, baseline: Dict) -> List[Dict]:
        deviations = []
        for metric_key, stats in baseline.items():
            if stats["std"] < 1e-9:
                continue
            parts = metric_key.split(".", 1)
            if len(parts) != 2:
                continue
            category, key = parts
            cat_data = session.get(category)
            if not cat_data:
                continue
            value = cat_data.get(key)
            if value is None or not isinstance(value, (int, float)):
                continue

            z = (float(value) - stats["mean"]) / stats["std"]
            if abs(z) > DEVIATION_THRESHOLD:
                deviations.append({
                    "metric": metric_key,
                    "value": float(value),
                    "baseline_mean": stats["mean"],
                    "baseline_std": stats["std"],
                    "z_score": round(z, 2),
                    "direction": "high" if z > 0 else "low",
                })
        return deviations

    def _check_clinical_patterns(self, deviations: List[Dict]) -> List[Dict]:
        deviation_map = {d["metric"]: d["direction"] for d in deviations}
        alerts = []

        for pattern_key, pattern in CLINICAL_PATTERNS.items():
            matching = 0
            details = []
            for indicator in pattern["indicators"]:
                metric = indicator["metric"]
                expected_dir = indicator["direction"]
                if deviation_map.get(metric) == expected_dir:
                    matching += 1
                    details.append(f"{metric}: {expected_dir.upper()}")

            if matching >= pattern["min_correlated"]:
                total = len(pattern["indicators"])
                severity = "critical" if matching >= pattern["min_correlated"] + 2 else "warning"
                alerts.append({
                    "pattern": pattern_key,
                    "name": pattern["name"],
                    "matching_indicators": matching,
                    "total_indicators": total,
                    "details": details,
                    "severity": severity,
                    "timestamp": datetime.now().isoformat(),
                })

        return alerts

    # ─── WAV Concatenation ──────────────────────────────────────────────────

    @staticmethod
    def concatenate_wavs(wav_dir: str, output_path: str) -> Optional[str]:
        wav_files = sorted(glob.glob(os.path.join(wav_dir, "*.wav")))
        if not wav_files:
            return None

        with wave.open(wav_files[0], "rb") as wf:
            params = wf.getparams()

        with wave.open(output_path, "wb") as out:
            out.setparams(params)
            for f in wav_files:
                try:
                    with wave.open(f, "rb") as wf:
                        out.writeframes(wf.readframes(wf.getnframes()))
                except Exception as e:
                    print(f"[VOICE] Skipping corrupt segment {f}: {e}")

        for f in wav_files:
            try:
                os.remove(f)
            except OSError:
                pass

        return output_path
