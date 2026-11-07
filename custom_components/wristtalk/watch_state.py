"""A paired watch's state: paired, or waiting to be paired again (with its repair issue)."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    ISSUE_NEEDS_PAIRING,
    WATCH_APP,
    WATCH_KEY_HASH,
    WATCH_MODEL,
    WATCH_NEEDS_PAIRING,
    WATCH_PAIRED_AT,
)
from .pairing import WatchRequest, hash_key


def issue_id(subentry_id: str) -> str:
    return f"{ISSUE_NEEDS_PAIRING}_{subentry_id}"


def paired_data(watch: WatchRequest) -> dict[str, str | bool]:
    """What a watch's subentry keeps after pairing: its key's hash, never the key."""
    assert watch.key is not None
    return {
        WATCH_KEY_HASH: hash_key(watch.key),
        WATCH_MODEL: watch.model,
        WATCH_APP: watch.app_version,
        WATCH_PAIRED_AT: dt_util.utcnow().isoformat(),
        WATCH_NEEDS_PAIRING: False,
    }


@callback
def async_paired_again(
    hass: HomeAssistant,
    entry: ConfigEntry,
    subentry: ConfigSubentry,
    watch: WatchRequest,
) -> None:
    """The same watch paired again: a new key in its subentry - the device, its entities and
    their settings (pipeline, voice quality) stay; the old key stops working."""
    hass.config_entries.async_update_subentry(
        entry, subentry, data={**subentry.data, **paired_data(watch)}
    )
    ir.async_delete_issue(hass, DOMAIN, issue_id(subentry.subentry_id))


@callback
def async_needs_pairing(
    hass: HomeAssistant, entry: ConfigEntry, subentry: ConfigSubentry
) -> None:
    """The watch asked to be paired again: its key is revoked at once, the watch shows that it
    needs pairing, and Repairs offer to do it."""
    hass.config_entries.async_update_subentry(
        entry,
        subentry,
        data={**subentry.data, WATCH_KEY_HASH: "", WATCH_NEEDS_PAIRING: True},
    )
    _create_issue(hass, entry, subentry)


def _create_issue(
    hass: HomeAssistant, entry: ConfigEntry, subentry: ConfigSubentry
) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(subentry.subentry_id),
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_NEEDS_PAIRING,
        translation_placeholders={"watch": subentry.title},
        data={"entry_id": entry.entry_id, "subentry_id": subentry.subentry_id},
    )


@callback
def async_sync_issues(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """At setup: an issue for each watch waiting to be paired again, none for the rest (a watch
    removed or paired meanwhile)."""
    waiting = {}
    for subentry in entry.subentries.values():
        if subentry.data.get(WATCH_NEEDS_PAIRING):
            waiting[issue_id(subentry.subentry_id)] = subentry
            _create_issue(hass, entry, subentry)
    registry = ir.async_get(hass)
    for domain, issue in list(registry.issues):
        if (
            domain == DOMAIN
            and issue.startswith(f"{ISSUE_NEEDS_PAIRING}_")
            and issue not in waiting
        ):
            ir.async_delete_issue(hass, DOMAIN, issue)
