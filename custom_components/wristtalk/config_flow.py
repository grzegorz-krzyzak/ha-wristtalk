"""Config flow: the integration (default pipeline, address) and "Add watch" (pairing)."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from homeassistant.components.assist_pipeline import async_get_pipelines
from homeassistant.components.assist_pipeline.pipeline import async_get_pipeline
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .const import (
    CONF_PIPELINE,
    CONF_URL,
    DOMAIN,
    SUBENTRY_WATCH,
    WATCH_APP,
    WATCH_KEY_HASH,
    WATCH_MODEL,
    WATCH_PAIRED_AT,
)
from .pairing import PairingError, WatchRequest, hash_key


def _pipeline_options(hass: HomeAssistant) -> list[SelectOptionDict]:
    return [
        SelectOptionDict(value=pipeline.id, label=pipeline.name)
        for pipeline in async_get_pipelines(hass)
    ]


def _preferred_pipeline(hass: HomeAssistant) -> str | None:
    try:
        return async_get_pipeline(hass).id
    except Exception:  # noqa: BLE001 - no pipelines yet
        return None


def _known_urls(hass: HomeAssistant) -> list[str]:
    """The addresses Home Assistant knows, HTTPS ones only (the watch needs HTTPS)."""
    urls: list[str] = []
    for kwargs in (
        {"allow_internal": False, "allow_cloud": False},  # external
        {"allow_external": False, "allow_cloud": False},  # internal
        {
            "allow_internal": False,
            "allow_external": False,
            "require_cloud": True,
        },  # cloud
    ):
        try:
            url = get_url(hass, require_ssl=True, **kwargs)
        except NoURLAvailableError:
            continue
        if url not in urls:
            urls.append(url)
    return urls


def _schema(hass: HomeAssistant, pipeline: str | None, url: str | None) -> vol.Schema:
    urls = _known_urls(hass)
    return vol.Schema(
        {
            vol.Required(CONF_PIPELINE, default=pipeline): SelectSelector(
                SelectSelectorConfig(
                    options=_pipeline_options(hass), mode=SelectSelectorMode.DROPDOWN
                )
            ),
            vol.Required(
                CONF_URL, default=url or (urls[0] if urls else "")
            ): SelectSelector(
                SelectSelectorConfig(
                    options=urls, custom_value=True, mode=SelectSelectorMode.DROPDOWN
                )
            ),
        }
    )


def _validate(user_input: dict[str, Any]) -> dict[str, str]:
    """HTTPS only: the watch reaches Home Assistant through the phone, iOS refuses plain HTTP."""
    url = str(user_input.get(CONF_URL, "")).strip()
    if not url.lower().startswith("https://") or len(url) <= len("https://"):
        return {CONF_URL: "https_required"}
    return {}


class WristTalkConfigFlow(ConfigFlow, domain=DOMAIN):
    """One WristTalk per Home Assistant."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(user_input)
            if not errors:
                return self.async_create_entry(
                    title="WristTalk",
                    data={},
                    options={
                        CONF_PIPELINE: user_input[CONF_PIPELINE],
                        CONF_URL: user_input[CONF_URL].strip().rstrip("/"),
                    },
                )
        return self.async_show_form(
            step_id="user",
            data_schema=_schema(
                self.hass,
                (user_input or {}).get(CONF_PIPELINE) or _preferred_pipeline(self.hass),
                (user_input or {}).get(CONF_URL),
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return WristTalkOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_WATCH: AddWatchFlow}


class WristTalkOptionsFlow(OptionsFlow):
    """The default pipeline for new watches and the address."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(user_input)
            if not errors:
                return self.async_create_entry(
                    data={
                        CONF_PIPELINE: user_input[CONF_PIPELINE],
                        CONF_URL: user_input[CONF_URL].strip().rstrip("/"),
                    }
                )
        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=_schema(
                self.hass,
                (user_input or {}).get(CONF_PIPELINE) or options.get(CONF_PIPELINE),
                (user_input or {}).get(CONF_URL) or options.get(CONF_URL),
            ),
            errors=errors,
        )


class AddWatchFlow(ConfigSubentryFlow):
    """Add watch: opens the pairing window, waits for the watch, shows its code to confirm.

    Closing the flow closes the window (async_remove) - no window left open in the background.
    """

    def __init__(self) -> None:
        self._wait_task: asyncio.Task[WatchRequest | None] | None = None
        self._watch: WatchRequest | None = None

    @property
    def _pairing(self):
        return self._get_entry().runtime_data.pairing

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        if self._wait_task is None:
            self._pairing.open(self.flow_id)
            self._wait_task = self.hass.async_create_task(
                self._pairing.async_wait_for_watch()
            )
        if not self._wait_task.done():
            return self.async_show_progress(
                step_id="user",
                progress_action="wait_watch",
                description_placeholders={
                    CONF_URL: self._get_entry().options.get(CONF_URL, "")
                },
                progress_task=self._wait_task,
            )
        self._watch = self._wait_task.result()
        if self._watch is None:
            return self.async_show_progress_done(next_step_id="timeout")
        return self.async_show_progress_done(next_step_id="confirm")

    async def async_step_timeout(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        self._pairing.close(self.flow_id)
        return self.async_abort(reason="timeout")

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        watch = self._watch
        assert watch is not None
        if user_input is None:
            return self.async_show_form(
                step_id="confirm",
                description_placeholders={
                    "code": f"{watch.code[:3]} {watch.code[3:]}",
                    "model": watch.model or "-",
                },
            )
        try:
            key = self._pairing.confirm(self.flow_id)
        except PairingError:
            return self.async_abort(reason="timeout")
        entry = self._get_entry()
        # Pairing the same watch again replaces it - its old key stops working
        for subentry in list(entry.subentries.values()):
            if subentry.unique_id == watch.unique_id:
                self.hass.config_entries.async_remove_subentry(
                    entry, subentry.subentry_id
                )
        return self.async_create_entry(
            title=watch.model or "Watch",
            unique_id=watch.unique_id,
            data={
                WATCH_KEY_HASH: hash_key(key),
                WATCH_MODEL: watch.model,
                WATCH_APP: watch.app_version,
                WATCH_PAIRED_AT: dt_util.utcnow().isoformat(),
            },
        )

    @callback
    def async_remove(self) -> None:
        """The owner closed the flow: close the window with it."""
        if self._wait_task is not None and not self._wait_task.done():
            self._wait_task.cancel()
        # The entry may be gone already
        with contextlib.suppress(Exception):
            self._pairing.close(self.flow_id)
