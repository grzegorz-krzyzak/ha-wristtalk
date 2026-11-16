"""The texts Home Assistant shows: no URLs written in them (hassfest refuses them) - a link comes in
as a placeholder from the code."""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

TRANSLATIONS = (
    Path(__file__).parent.parent / "custom_components" / "wristtalk" / "translations"
)
# hassfest's rule (script/hassfest/translations.py, RE_URL)
URL = re.compile(
    r"(((ftp|ftps|scp|http|https|mqtt|mqtts|socket|socks5):\/\/|www\.)"
    r"[a-z0-9]+([\-\.]{1}[a-z0-9]+)*\.[a-z]{2,5}(:[0-9]{1,5})?(\/.*)?)",
    re.IGNORECASE,
)


def _strings(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(value, f"{path}.{key}" if path else key)
    elif isinstance(node, str):
        yield path, node


@pytest.mark.parametrize("language", ["en", "pl"])
def test_no_urls_in_translations(language: str) -> None:
    texts = json.loads((TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8"))
    found = [path for path, text in _strings(texts) if URL.search(text)]
    assert not found, f"URLs in {language}.json at {found}; use a placeholder"
