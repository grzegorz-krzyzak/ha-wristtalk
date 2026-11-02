"""Fixtures: fake Assist pipelines, an example Home Assistant address, a set-up WristTalk."""

from __future__ import annotations

from dataclasses import dataclass
import sys
import types
from unittest.mock import MagicMock, patch

# Assist's audio libraries (VAD, noise suppression) have no wheels for Python 3.14 on macOS and do
# not build there; the tests fake the pipeline, so stand-ins are enough when the real ones are
# missing. On Linux CI the real packages install normally.
for _name, _attrs in (("pymicro_vad", ("MicroVad",)), ("pyspeex_noise", ("AudioProcessor",))):
    try:
        __import__(_name)
    except ImportError:
        _module = types.ModuleType(_name)
        for _attr in _attrs:
            setattr(_module, _attr, MagicMock())
        sys.modules[_name] = _module

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.setup import async_setup_component

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wristtalk.const import CONF_PIPELINE, CONF_URL, DOMAIN

EXTERNAL_URL = "https://home.example.org"
INTERNAL_URL = "http://192.0.2.10:8123"


@dataclass
class FakePipeline:
    id: str
    name: str


PIPELINES = [FakePipeline("p-claude", "Claude"), FakePipeline("p-local", "Local")]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
async def core_components(hass: HomeAssistant) -> None:
    """The voice components WristTalk depends on need Home Assistant's own core component."""
    assert await async_setup_component(hass, "homeassistant", {})


@pytest.fixture(autouse=True)
def fake_pipelines():
    """Assist pipelines without setting up the whole voice stack."""
    with (
        patch("custom_components.wristtalk.config_flow.async_get_pipelines", return_value=PIPELINES),
        patch("custom_components.wristtalk.config_flow.async_get_pipeline", return_value=PIPELINES[0]),
    ):
        yield


@pytest.fixture
async def urls(hass: HomeAssistant) -> None:
    await async_process_ha_core_config(
        hass, {"external_url": EXTERNAL_URL, "internal_url": INTERNAL_URL}
    )


@pytest.fixture
async def entry(hass: HomeAssistant, urls) -> MockConfigEntry:
    """WristTalk set up, with the http component for the watch endpoints."""
    await async_setup_component(hass, "http", {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="WristTalk",
        options={CONF_PIPELINE: "p-claude", CONF_URL: EXTERNAL_URL},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
