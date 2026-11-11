"""Tests for the Rivian actions."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol

from custom_components.rivian import services
from custom_components.rivian.const import (
    ATTR_COORDINATOR,
    ATTR_USER,
    ATTR_VEHICLE,
    DOMAIN,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError


def call(
    accepted: bool = True, device: str = "device"
) -> tuple[MagicMock, AsyncMock, MagicMock]:
    """A call for a vehicle whose device is "device"."""
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


async def test_send_trip() -> None:
    """Stops are sent from the vehicle's location; without one, it raises."""
    service_call, _, registry = call()
    coor = service_call.hass.data[DOMAIN]["entry"][ATTR_COORDINATOR][ATTR_VEHICLE][
        "vehicle"
    ]
    coor.api.send_trip_to_vehicle = AsyncMock(return_value=True)
    stops = [{"name": "Stadium", "latitude": 39.05, "longitude": -94.48}]
    service_call.data = {
        "device_id": "device",
        "stops": stops,
        "min_arrival_charge": 30,
    }
    with patch.object(services.dr, "async_get", return_value=registry):
        coor.data = {"gnssLocation": {"latitude": 40.1, "longitude": -92.5}}
        await services._send_trip(service_call)
        coor.api.send_trip_to_vehicle.assert_awaited_once_with(
            "vehicle", (40.1, -92.5), [("Stadium", 39.05, -94.48)], 30, None
        )
        coor.data = {}
        with pytest.raises(HomeAssistantError):
            await services._send_trip(service_call)


async def test_end_trip() -> None:
    """The trip is ended for the signed-in user."""
    service_call, _, registry = call()
    entry = service_call.hass.data[DOMAIN]["entry"]
    coor = entry[ATTR_COORDINATOR][ATTR_VEHICLE]["vehicle"]
    coor.config_entry = MagicMock(entry_id="entry")
    coor.api.end_trip = AsyncMock(return_value=True)
    entry[ATTR_COORDINATOR][ATTR_USER] = SimpleNamespace(data={"id": "user"})
    service_call.data = {"device_id": "device"}
    with patch.object(services.dr, "async_get", return_value=registry):
        await services._end_trip(service_call)
    coor.api.end_trip.assert_awaited_once_with("vehicle", "user")


@pytest.mark.parametrize("action", ["send_trip", "end_trip"])
async def test_trip_refused(action: str) -> None:
    """A trip, or ending one, that Rivian refuses fails the action."""
    service_call, _, registry = call()
    entry = service_call.hass.data[DOMAIN]["entry"]
    coor = entry[ATTR_COORDINATOR][ATTR_VEHICLE]["vehicle"]
    coor.config_entry = MagicMock(entry_id="entry")
    coor.data = {"gnssLocation": {"latitude": 40.1, "longitude": -92.5}}
    coor.api.send_trip_to_vehicle = coor.api.end_trip = AsyncMock(return_value=False)
    entry[ATTR_COORDINATOR][ATTR_USER] = SimpleNamespace(data={"id": "user"})
    service_call.data = {
        "device_id": "device",
        "stops": [{"name": "Stadium", "latitude": 39.05, "longitude": -94.48}],
        "min_arrival_charge": 20,
    }
    with (
        patch.object(services.dr, "async_get", return_value=registry),
        pytest.raises(HomeAssistantError),
    ):
        await getattr(services, f"_{action}")(service_call)


def test_charging_networks_are_validated() -> None:
    """Unknown network names are rejected before reaching the library."""
    networks = services.SEND_TRIP_SCHEMA.schema[vol.Optional("charging_networks")]
    assert networks("tesla") == ["tesla"]
    with pytest.raises(vol.Invalid):
        networks(["ea"])
