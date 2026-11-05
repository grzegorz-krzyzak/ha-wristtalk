"""Constants of the WristTalk integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "wristtalk"

CONF_PIPELINE: Final = "pipeline"
CONF_URL: Final = "url"

SUBENTRY_WATCH: Final = "watch"

# Subentry data of a paired watch
WATCH_KEY_HASH: Final = "key_hash"
WATCH_MODEL: Final = "model"
WATCH_APP: Final = "app_version"
WATCH_PAIRED_AT: Final = "paired_at"

# Protocol watch <-> integration: raised on every change one side sends or expects from the other
# (the compatibility table is in the design notes). 2: every wait within 4 s, audio 202 while it is
# being made. The app is not public yet, so protocol 1 is no longer taken: an older app hears
# "update the app" instead of working worse
PROTOCOL: Final = 2
MIN_PROTOCOL: Final = 2
API_BASE: Final = "/api/wristtalk/v1"
HEADER_PROTOCOL: Final = "X-WT-Protocol"
HEADER_APP: Final = "X-WT-App"
# Not "Authorization" - Home Assistant's own authentication would try to read it
HEADER_KEY: Final = "X-WT-Key"
HEADER_TEXT_ONLY: Final = (
    "X-WT-Text-Only"  # first chunk: "1" = the pipeline ends at the agent
)
HEADER_FINAL: Final = "X-WT-Final"  # "1" = the user ended the command (START)

# The reply's voice, per watch: Opus bitrate - smaller arrives sooner through the phone (about
# 13 kB/s on a fēnix 8), larger sounds better
# Heard on the watch speaker (2026-11-05): hardly any difference, the house status 20 / 30 / 43 kB -
# so "fast" is the default
SPEECH_QUALITY_BITRATES: Final = {"fast": "10k", "balanced": "16k", "best": "24k"}
SPEECH_QUALITY_DEFAULT: Final = "fast"

# Pairing
PAIRING_WINDOW: Final = timedelta(minutes=5)
PAIR_CALLS_PER_WINDOW: Final = 10
CODE_DIGITS: Final = 6
KEY_BYTES: Final = 32
