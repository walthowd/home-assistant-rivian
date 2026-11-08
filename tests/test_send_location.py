"""Tests for the send_location action."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.rivian import services
from custom_components.rivian.const import ATTR_COORDINATOR, ATTR_VEHICLE, DOMAIN
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError


def call(
    accepted: bool = True, device: str = "device"
) -> tuple[MagicMock, AsyncMock, MagicMock]:
    """A send_location call for a vehicle whose device is "device"."""
    result = {"publishResponse": {"result": 0}} if accepted else None
    response = MagicMock(
        json=AsyncMock(
            return_value={"data": {"parseAndShareLocationToVehicle": result}}
        )
    )
    share = AsyncMock(return_value=response)
    coor = SimpleNamespace(
        vehicle_id="vehicle", api=SimpleNamespace(send_location_to_vehicle=share)
    )
    hass = MagicMock()
    hass.data = {
        DOMAIN: {"entry": {ATTR_COORDINATOR: {ATTR_VEHICLE: {"vehicle": coor}}}}
    }
    registry = MagicMock()
    registry.async_get.side_effect = lambda device_id: (
        SimpleNamespace(identifiers={(DOMAIN, "vehicle")})
        if device_id == "device"
        else None
    )
    service_call = MagicMock(
        hass=hass,
        data={"device_id": device, "latitude": 39.05, "longitude": -94.48},
    )
    return service_call, share, registry


async def test_send_location() -> None:
    """The coordinates are sent as a map link to the device's vehicle."""
    service_call, share, registry = call()
    with patch.object(services.dr, "async_get", return_value=registry):
        await services._send_location(service_call)
    share.assert_awaited_once_with(
        "vehicle",
        "https://www.google.com/maps/search/?api=1&query=39.05,-94.48",
    )


@pytest.mark.parametrize(
    ("accepted", "device", "error"),
    [(False, "device", HomeAssistantError), (True, "other", ServiceValidationError)],
)
async def test_send_location_fails(
    accepted: bool, device: str, error: type[Exception]
) -> None:
    """A refused location fails; a device that isn't a vehicle is invalid input."""
    service_call, _, registry = call(accepted, device)
    with (
        patch.object(services.dr, "async_get", return_value=registry),
        pytest.raises(error),
    ):
        await services._send_location(service_call)


async def test_send_location_api_error() -> None:
    """An API error, e.g. a rate limit, fails the action like a refusal."""
    service_call, share, registry = call()
    share.side_effect = services.RivianApiException("rate limited")
    with (
        patch.object(services.dr, "async_get", return_value=registry),
        pytest.raises(HomeAssistantError),
    ):
        await services._send_location(service_call)
