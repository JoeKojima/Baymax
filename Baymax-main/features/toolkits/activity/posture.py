"""Per-sample posture features from MediaPipe Pose landmarks. Pure functions, no I/O.

Inputs are plain tuples so tests can build them by hand:
- `image`: 33 x (x, y, z, visibility), x/y normalised to the frame (0-1, y down).
- `world`: 33 x (x, y, z) in metres around the hips, y down (MediaPipe world
  landmarks). Optional; they make sitting vs. standing far less sensitive to the
  camera angle, because a thigh pointing at the camera looks short in 2D.

The thresholds are first guesses, to be tuned against the shadow-mode diary.
"""
import math

NOSE, MOUTH_L, MOUTH_R = 0, 9, 10
SHOULDERS, HIPS, KNEES, ANKLES = (11, 12), (23, 24), (25, 26), (27, 28)
WRISTS, INDEX = (15, 16), (19, 20)
VISIBLE = 0.5

LYING_TRUNK_DEG = 60.0       # Torso this far from vertical = lying (the fall detector uses 60 too).
SITTING_THIGH_DEG = 50.0     # Thigh this far from vertical = sitting.
STANDING_THIGH_DEG = 30.0    # Thigh this close to vertical (and knee straight) = standing.
SITTING_KNEE_DEG = 125.0     # Knee bent more than this = sitting.
STANDING_KNEE_DEG = 145.0
HAND_NEAR_MOUTH = 0.5        # Hand within this many shoulder widths of the mouth.

POSTURES = ("standing", "walking", "sitting", "lying", "upright", "absent")
NEUTRAL = "unknown"  # Person seen but torso hidden: no vote either way.


def _visible(image, *indices):
    return all(image[i][3] >= VISIBLE for i in indices)


def _mid(points, a, b):
    return tuple((points[a][k] + points[b][k]) / 2 for k in range(3))


def _angle_from_down(vector):
    """Degrees between a vector and straight down (+y)."""
    length = math.sqrt(sum(c * c for c in vector))
    if length < 1e-6:
        return None
    return math.degrees(math.acos(max(-1.0, min(1.0, vector[1] / length))))


def _joint_angle(a, b, c):
    """Angle at b, in degrees, between b->a and b->c."""
    u = [a[k] - b[k] for k in range(len(a))]
    v = [c[k] - b[k] for k in range(len(c))]
    lu, lv = math.sqrt(sum(x * x for x in u)), math.sqrt(sum(x * x for x in v))
    if lu < 1e-6 or lv < 1e-6:
        return None
    return math.degrees(math.acos(max(-1.0, min(1.0, sum(x * y for x, y in zip(u, v)) / (lu * lv)))))


def _scaled(image, aspect):
    """Image points with x scaled so distances are the same in x and y."""
    return [(p[0] * aspect, p[1], 0.0) for p in image]


def features(image, world=None, aspect=4 / 3):
    """Posture features for one pose sample, or None when the torso isn't visible."""
    if image is None or len(image) < 33 or not _visible(image, *SHOULDERS, *HIPS):
        return None
    flat = _scaled(image, aspect)
    points = world if world is not None and len(world) >= 33 else flat
    shoulder, hip = _mid(points, *SHOULDERS), _mid(points, *HIPS)
    trunk = _angle_from_down(tuple(h - s for s, h in zip(shoulder, hip)))  # shoulders -> hips points down
    thighs, knees = [], []
    for h, k, a in zip(HIPS, KNEES, ANKLES):
        if _visible(image, h, k):
            thighs.append(_angle_from_down(tuple(points[k][i] - points[h][i] for i in range(3))))
            if _visible(image, a):
                knees.append(_joint_angle(points[h], points[k], points[a]))
    thighs = [t for t in thighs if t is not None]
    knees = [k for k in knees if k is not None]
    fs, fh = _mid(flat, *SHOULDERS), _mid(flat, *HIPS)
    torso_px = math.dist(fs[:2], fh[:2])
    shoulder_width = math.dist(flat[SHOULDERS[0]][:2], flat[SHOULDERS[1]][:2])
    return {
        "trunk_deg": trunk,
        "thigh_deg": sum(thighs) / len(thighs) if thighs else None,
        "knee_deg": sum(knees) / len(knees) if knees else None,
        "hip": fh[:2],
        "torso": max(torso_px, 1e-3),
        "hand_near_mouth": hand_near_mouth(image, flat, shoulder_width),
        "points": [p[:2] if image[i][3] >= VISIBLE else None for i, p in enumerate(flat)],
    }


def hand_near_mouth(image, flat, shoulder_width):
    """True when a visible wrist or index finger is close to the mouth."""
    if shoulder_width < 1e-3 or not _visible(image, MOUTH_L, MOUTH_R):
        return False
    mouth = _mid(flat, MOUTH_L, MOUTH_R)[:2]
    for i in (*WRISTS, *INDEX):
        if image[i][3] >= VISIBLE and math.dist(flat[i][:2], mouth) < HAND_NEAR_MOUTH * shoulder_width:
            return True
    return False


def classify(feature):
    """standing, sitting, lying, upright (torso upright, legs hidden or in between) or unknown."""
    if feature is None:
        return NEUTRAL
    trunk, thigh, knee = feature["trunk_deg"], feature["thigh_deg"], feature["knee_deg"]
    if trunk is not None and trunk > LYING_TRUNK_DEG:
        return "lying"
    if thigh is None:
        return "upright"
    if thigh > SITTING_THIGH_DEG or (knee is not None and knee < SITTING_KNEE_DEG):
        return "sitting"
    if thigh < STANDING_THIGH_DEG and (knee is None or knee > STANDING_KNEE_DEG):
        return "standing"
    return "upright"


def movement(previous, current, seconds):
    """Mean movement of the visible joints, in torso lengths per second, and hip speed."""
    if previous is None or current is None or seconds <= 0:
        return None, None
    moves = [math.dist(a, b) for a, b in zip(previous["points"], current["points"]) if a and b]
    torso = current["torso"]
    body = (sum(moves) / len(moves)) / torso / seconds if moves else None
    hip = math.dist(previous["hip"], current["hip"]) / torso / seconds
    return body, hip
