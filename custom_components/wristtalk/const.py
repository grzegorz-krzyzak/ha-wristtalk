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

# Protocol watch <-> integration
PROTOCOL: Final = 1
MIN_PROTOCOL: Final = 1
API_BASE: Final = "/api/wristtalk/v1"
HEADER_PROTOCOL: Final = "X-WT-Protocol"
HEADER_APP: Final = "X-WT-App"
# Not "Authorization" - Home Assistant's own authentication would try to read it
HEADER_KEY: Final = "X-WT-Key"
HEADER_TEXT_ONLY: Final = "X-WT-Text-Only"  # first chunk: "1" = the pipeline ends at the agent
HEADER_FINAL: Final = "X-WT-Final"  # "1" = the user ended the command (START)

# Pairing
PAIRING_WINDOW: Final = timedelta(minutes=5)
PAIR_CALLS_PER_WINDOW: Final = 10
CODE_DIGITS: Final = 6
KEY_BYTES: Final = 32
