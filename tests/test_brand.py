"""The integration ships its own icon (no Home Assistant branded images)."""

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration


async def test_has_branding(hass: HomeAssistant) -> None:
    integration = await async_get_integration(hass, "wristtalk")
    assert integration.has_branding
