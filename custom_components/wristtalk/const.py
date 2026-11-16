"""Constants of the WristTalk integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "wristtalk"

CONF_PIPELINE: Final = "pipeline"
CONF_CODE: Final = "code"
CONF_URL: Final = "url"

# Under the address field (setup and options); Home Assistant's translations take no URLs, so it is a
# placeholder there
SPONSORS_URL: Final = "https://github.com/sponsors/grzegorz-krzyzak"

SUBENTRY_WATCH: Final = "watch"

# Subentry data of a paired watch
WATCH_KEY_HASH: Final = "key_hash"
WATCH_MODEL: Final = "model"
WATCH_APP: Final = "app_version"
WATCH_PAIRED_AT: Final = "paired_at"
# The watch asked to be paired again ("Pair again" in its menu): its key is revoked until then
WATCH_NEEDS_PAIRING: Final = "needs_pairing"

# Repair issue of a watch that needs pairing again (one per watch, "<key>_<subentry id>")
ISSUE_NEEDS_PAIRING: Final = "needs_pairing"

# Protocol watch <-> integration: raised on every change one side sends or expects from the other.
# 2: every wait within 4 s, audio 202 while it is being made. 3: `POST repair` - the watch's "Pair
# again"; an app on protocol 2 does not send it and keeps working. 4: `final` sent at once, the
# input closed only with the whole recording. 5: the chunk answer "recognizing". 6: `GET
# audio/<session>/<offset>` - the voice in pieces while it is being made; apps on 2-5 get it whole
# as before. Protocol 1 is not taken: an app that old hears "update the app" instead of working worse
PROTOCOL: Final = 6
# From this protocol on the watch knows the chunk answer "recognizing" (the command has ended)
RECOGNIZING_PROTOCOL: Final = 5
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

# Fired when a pipeline run of a watch command starts: the command's session (as the watch names it)
# and the run's start, so a log of pipeline runs can count them per command
EVENT_COMMAND_RUN: Final = "wristtalk_command_run"

# The reply's voice, per watch: Opus bitrate - smaller arrives sooner through the phone (about
# 13 kB/s on a fēnix 8), larger sounds better
# "balanced" is the default: on the fēnix 8's speaker "fast" sounds rougher, "balanced" clearly
# better; with the voice in pieces it costs only the first piece - about 1.5-2 kB more, 0.15 s by
# the phone
SPEECH_QUALITY_BITRATES: Final = {"fast": "10k", "balanced": "16k", "best": "24k"}
SPEECH_QUALITY_DEFAULT: Final = "balanced"

# Pairing
# 3 minutes: enough to go to Home Assistant and type the code (with the code typed a stranger gets
# no key anyway)
PAIRING_WINDOW: Final = timedelta(minutes=3)
PAIR_CALLS_PER_WINDOW: Final = 10
CODE_DIGITS: Final = 6
# Wrong codes typed in one window before it closes
CODE_TRIES: Final = 3
KEY_BYTES: Final = 32
