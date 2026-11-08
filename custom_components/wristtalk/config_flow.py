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
    ConfigSubentry,
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

from . import watch_state
from .const import (
    CONF_CODE,
    CONF_PIPELINE,
    CONF_URL,
    DOMAIN,
    SUBENTRY_WATCH,
)
from .pairing import PairingError, WatchRequest


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
            # The address in full, as a link under its field: the dropdown cuts a long one, and on a phone the
            # link opens in the browser, where the address bar copies it in one touch
            description_placeholders={CONF_URL: options.get(CONF_URL) or "-"},
        )


class PairingSteps:
    """Shared by "Add watch", "Pair again" (a watch's menu) and the repair: open the pairing window,
    wait for a watch, take the code it shows. Closing the flow closes the window (async_remove) -
    no window left open in the background."""

    hass: HomeAssistant
    flow_id: str
    _wait_task: asyncio.Task[bool] | None = None

    def _wristtalk_entry(self) -> ConfigEntry:
        raise NotImplementedError

    def _target(self) -> ConfigSubentry | None:
        """The watch being paired again, or None for any watch."""
        return None

    def _finish(self, watch: WatchRequest) -> Any:
        raise NotImplementedError

    def _reason(self, reason: str) -> str:
        """An ending that sends the owner back where they started (Add watch, the ⚙, Fix)."""
        return reason

    @property
    def _pairing(self):
        return self._wristtalk_entry().runtime_data.pairing

    def _wait(self, step_id: str) -> Any:
        if self._wait_task is None:
            target = self._target()
            self._pairing.open(self.flow_id, target.unique_id if target else None)
            self._wait_task = self.hass.async_create_task(
                self._pairing.async_wait_for_watch()
            )
        if not self._wait_task.done():
            target = self._target()
            return self.async_show_progress(  # type: ignore[attr-defined]
                step_id=step_id,
                progress_action="wait_watch_again" if target else "wait_watch",
                description_placeholders={
                    CONF_URL: self._wristtalk_entry().options.get(CONF_URL, ""),
                    "watch": target.title if target else "",
                },
                progress_task=self._wait_task,
            )
        if not self._wait_task.result():
            return self.async_show_progress_done(next_step_id="timeout")  # type: ignore[attr-defined]
        return self.async_show_progress_done(next_step_id="code")  # type: ignore[attr-defined]

    async def async_step_timeout(self, user_input: dict[str, Any] | None = None) -> Any:
        self._pairing.close(self.flow_id)
        return self.async_abort(reason=self._reason("timeout"))  # type: ignore[attr-defined]

    async def async_step_code(self, user_input: dict[str, Any] | None = None) -> Any:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                watch = self._pairing.confirm(self.flow_id, str(user_input[CONF_CODE]))
            except PairingError as err:
                if err.code in ("invalid_code", "invalid_format", "wrong_watch"):
                    errors[CONF_CODE] = err.code
                elif err.code == "too_many_codes":
                    return self.async_abort(reason=self._reason("too_many_codes"))  # type: ignore[attr-defined]
                else:
                    # The window ended (or another flow took it) while the code was being typed
                    return self.async_abort(reason=self._reason("expired"))  # type: ignore[attr-defined]
            else:
                return self._finish(watch)
        watches = "\n".join(
            f"- {request.model or '-'} ({dt_util.as_local(request.requested_at):%H:%M:%S})"
            for request in self._pairing.requests
        )
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="code",
            data_schema=vol.Schema({vol.Required(CONF_CODE, default=""): str}),
            description_placeholders={"watches": watches or "-"},
            errors=errors,
        )

    @callback
    def async_remove(self) -> None:
        """The owner closed the flow: close the window with it."""
        if self._wait_task is not None and not self._wait_task.done():
            self._wait_task.cancel()
        # The entry may be gone already
        with contextlib.suppress(Exception):
            self._pairing.close(self.flow_id)


class AddWatchFlow(PairingSteps, ConfigSubentryFlow):
    """Add watch, and "Pair again" from a watch's menu: either way a watch already known gets a new
    key in its subentry - its device, entities and settings stay."""

    def _wristtalk_entry(self) -> ConfigEntry:
        return self._get_entry()

    def _target(self) -> ConfigSubentry | None:
        return (
            self._get_reconfigure_subentry() if self.source == "reconfigure" else None
        )

    def _reason(self, reason: str) -> str:
        # "Pair again" (the ⚙) has its own endings: "click the ⚙ again", not "Add watch"
        return f"{reason}_again" if self.source == "reconfigure" else reason

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return self._wait("user")

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return self._wait("reconfigure")

    def _finish(self, watch: WatchRequest) -> SubentryFlowResult:
        entry = self._get_entry()
        for subentry in entry.subentries.values():
            if subentry.unique_id == watch.unique_id:
                watch_state.async_paired_again(self.hass, entry, subentry, watch)
                return self.async_abort(reason="paired_again")
        return self.async_create_entry(
            title=watch.model or "Watch",
            unique_id=watch.unique_id,
            data=watch_state.paired_data(watch),
        )
