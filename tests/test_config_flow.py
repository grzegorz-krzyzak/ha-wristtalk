"""Adding the integration and its options: default pipeline and an HTTPS address."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wristtalk.const import CONF_PIPELINE, CONF_URL, DOMAIN

from .conftest import EXTERNAL_URL


async def test_user_flow_offers_https_addresses_and_preferred_pipeline(hass: HomeAssistant, urls) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    schema = result["data_schema"].schema
    fields = {str(key): key for key in schema}
    assert fields[CONF_PIPELINE].default() == "p-claude"
    assert fields[CONF_URL].default() == EXTERNAL_URL
    # The plain-http internal address is not offered
    assert schema[fields[CONF_URL]].config["options"] == [EXTERNAL_URL]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PIPELINE: "p-local", CONF_URL: EXTERNAL_URL + "/"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {CONF_PIPELINE: "p-local", CONF_URL: EXTERNAL_URL}


async def test_user_flow_refuses_plain_http(hass: HomeAssistant, urls) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    for url in ("http://192.0.2.10:8123", "home.example.org", "https://"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PIPELINE: "p-claude", CONF_URL: url}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {CONF_URL: "https_required"}


async def test_no_known_address_leaves_the_field_empty(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    fields = {str(key): key for key in result["data_schema"].schema}
    assert fields[CONF_URL].default() == ""


async def test_single_instance(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_options_change_pipeline_and_address(hass: HomeAssistant, entry) -> None:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_PIPELINE: "p-local", CONF_URL: "http://192.0.2.10:8123"}
    )
    assert result["errors"] == {CONF_URL: "https_required"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_PIPELINE: "p-local", CONF_URL: "https://other.example.org"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_PIPELINE: "p-local", CONF_URL: "https://other.example.org"}


async def test_no_pipelines_yet(hass: HomeAssistant, urls) -> None:
    with patch(
        "custom_components.wristtalk.config_flow.async_get_pipeline", side_effect=KeyError("none")
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    fields = {str(key): key for key in result["data_schema"].schema}
    assert fields[CONF_PIPELINE].default() is None
