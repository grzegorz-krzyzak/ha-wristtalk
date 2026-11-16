"""The middle number of the version is the protocol number - permanently, on both sides."""

import json
from pathlib import Path

from custom_components.wristtalk.const import PROTOCOL

MANIFEST = (
    Path(__file__).parents[1] / "custom_components" / "wristtalk" / "manifest.json"
)


def test_the_middle_number_is_the_protocol() -> None:
    _, minor, _ = (
        int(part) for part in json.loads(MANIFEST.read_text())["version"].split(".")
    )
    assert minor == PROTOCOL
