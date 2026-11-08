"""The version follows the rules in docs/design.md, section 5 (the only place they are written)."""

import json
from pathlib import Path

from custom_components.wristtalk.const import PROTOCOL

MANIFEST = (
    Path(__file__).parents[1] / "custom_components" / "wristtalk" / "manifest.json"
)


def test_until_1_0_the_middle_number_is_the_protocol() -> None:
    major, minor, _ = (
        int(part) for part in json.loads(MANIFEST.read_text())["version"].split(".")
    )
    if major == 0:
        assert minor == PROTOCOL
