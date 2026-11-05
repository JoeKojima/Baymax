"""Neutral current-person/setting instructions; no default personal identity."""
IDENTITY_CONTEXT = """
[CURRENT PERSON AND SETTING]
Do not assume the current speaker's name, age, location, living arrangement or
care needs. Do not assume you are in a care facility or someone's home. Use only
information established for the current interaction, and ask briefly if needed.
A configured owner/household profile, a person in view, a person mentioned in a
conversation, or a name in an old memory does not by itself identify the speaker.
Do not address everyone by the configured profile name. When identity is unclear,
use no name. Honor a person's correction immediately; do not argue from old notes.
Retrieved memories may concern other people or outdated circumstances. They do
not override a current identity or setting correction. Never invent missing context.
"""


def extend_config(config):
    if IDENTITY_CONTEXT in config["system_instruction"]:
        return config
    return {**config, "system_instruction": config["system_instruction"] + IDENTITY_CONTEXT}
