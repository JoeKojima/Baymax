"""Grouped, non-secret feature switches shared by Setup and Monitor."""
import os

SWITCHES = {
    'startup_greeting': ('BAYMAX_STARTUP_GREETING', True),
    'voice_sleep': ('BAYMAX_ENGAGE_WORDS', True),
    'pose_tracking': ('EMBER_POSE_TRACKING', True),
    'builtin_fall_detection': ('EMBER_BUILTIN_FALL_DETECTION', True),
    'eyes': ('BAYMAX_EYES_ENABLED', True),
}
GROUPS = (
    ('Startup', 'startup', ('startup_greeting',)),
    ('Conversation', 'conversation', ('voice_sleep',)),
    ('Memory Palace', 'memory_palace', ()),
    ('People Recognition', 'person_recognition', ()),
    ('Pose Detection', 'pose_detection', ('pose_tracking', 'builtin_fall_detection')),
    ('Eyes', 'eyes', ('eyes',)),
    ('Tools', 'toolkits', ()),
    ('Device Settings', None, ()),
)


def default_value(name):
    if name in ('pose_tracking', 'builtin_fall_detection'):
        import device_config
        key = 'camera.pose_tracking' if name == 'pose_tracking' else 'fall_detection.enabled'
        return device_config.get(key)
    return SWITCHES[name][1]


def enabled(name, default=None):
    env, _ = SWITCHES[name]
    return os.getenv(env, '1' if (default_value(name) if default is None else default) else '0') == '1'


def public(values):
    return {name: values.get(env, '1' if default_value(name) else '0') == '1'
            for name, (env, _) in SWITCHES.items()}


def validate(values):
    if not isinstance(values, dict) or set(values) - SWITCHES.keys():
        raise ValueError('Choose feature switches from the supplied controls.')
    if any(type(value) is not bool for value in values.values()):
        raise ValueError('Feature switches must be true or false.')
    return {SWITCHES[name][0]: '1' if value else '0' for name, value in values.items()}


def groups():
    return [{'name': name, 'switches': {key: enabled(key) for key in switches},
             'state': 'Packaged; disabled' if package in ('memory_palace', 'person_recognition') else 'Available'}
            for name, package, switches in GROUPS]
