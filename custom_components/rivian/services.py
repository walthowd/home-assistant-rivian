"""Rivian actions."""

from __future__ import annotations

from rivian.exceptions import RivianApiException
from rivian.rivian import CHARGING_NETWORKS
import voluptuous as vol

from homeassistant.const import ATTR_DEVICE_ID, ATTR_LATITUDE, ATTR_LONGITUDE
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr

from .const import ATTR_COORDINATOR, ATTR_USER, ATTR_VEHICLE, DOMAIN
from .coordinator import VehicleCoordinator

SERVICE_END_TRIP = "end_trip"
SERVICE_SEND_LOCATION = "send_location"
SERVICE_SEND_TRIP = "send_trip"

SEND_LOCATION_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_LATITUDE): cv.latitude,
        vol.Required(ATTR_LONGITUDE): cv.longitude,
    }
)

STOP_SCHEMA = vol.Schema(
    {
        vol.Required("name"): cv.string,
        vol.Required(ATTR_LATITUDE): cv.latitude,
        vol.Required(ATTR_LONGITUDE): cv.longitude,
    }
)
SEND_TRIP_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required("stops"): vol.All(
            cv.ensure_list, [STOP_SCHEMA], vol.Length(min=1)
        ),
        vol.Optional("min_arrival_charge", default=20): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=100)
        ),
        vol.Optional("charging_networks"): vol.All(
            cv.ensure_list, [vol.In(CHARGING_NETWORKS)]
        ),
    }
)
END_TRIP_SCHEMA = vol.Schema({vol.Required(ATTR_DEVICE_ID): cv.string})


def _vehicle(hass: HomeAssistant, device_id: str) -> VehicleCoordinator:
    """Return the coordinator of a vehicle's device."""
    if device := dr.async_get(hass).async_get(device_id):
        ids = {id_ for domain, id_ in device.identifiers if domain == DOMAIN}
        for data in hass.data.get(DOMAIN, {}).values():
            for vehicle_id, coor in data[ATTR_COORDINATOR][ATTR_VEHICLE].items():
                if vehicle_id in ids:
                    return coor
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="invalid_device",
        translation_placeholders={"device_id": device_id},
    )


async def _send_location(call: ServiceCall) -> None:
    """Send a location to the vehicle's navigation."""
    coor = _vehicle(call.hass, call.data[ATTR_DEVICE_ID])
    latitude, longitude = call.data[ATTR_LATITUDE], call.data[ATTR_LONGITUDE]
    url = f"https://www.google.com/maps/search/?api=1&query={latitude},{longitude}"
    try:
        response = await coor.api.send_location_to_vehicle(coor.vehicle_id, url)
        data = (await response.json()).get("data") or {}
    except RivianApiException as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="location_rejected"
        ) from err
    share = data.get("parseAndShareLocationToVehicle") or {}
    # result is 0 when Rivian accepted the location
    if (share.get("publishResponse") or {}).get("result") != 0:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="location_rejected"
        )


async def _send_trip(call: ServiceCall) -> None:
    """Send stops to the vehicle, which plans the route and charging stops."""
    coor = _vehicle(call.hass, call.data[ATTR_DEVICE_ID])
    location = (coor.data or {}).get("gnssLocation") or {}
    origin = (location.get("latitude"), location.get("longitude"))
    if None in origin:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="location_unknown"
        )
    if not await coor.api.send_trip_to_vehicle(
        coor.vehicle_id,
        origin,
        [(s["name"], s[ATTR_LATITUDE], s[ATTR_LONGITUDE]) for s in call.data["stops"]],
        call.data["min_arrival_charge"],
        call.data.get("charging_networks"),
    ):
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="trip_rejected"
        )


async def _end_trip(call: ServiceCall) -> None:
    """End the trip the vehicle is navigating."""
    coor = _vehicle(call.hass, call.data[ATTR_DEVICE_ID])
    entry_data = call.hass.data[DOMAIN][coor.config_entry.entry_id]
    user_id = entry_data[ATTR_COORDINATOR][ATTR_USER].data["id"]
    if not await coor.api.end_trip(coor.vehicle_id, user_id):
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="end_trip_rejected"
        )


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the Rivian actions."""
    for service, handler, schema in (
        (SERVICE_END_TRIP, _end_trip, END_TRIP_SCHEMA),
        (SERVICE_SEND_LOCATION, _send_location, SEND_LOCATION_SCHEMA),
        (SERVICE_SEND_TRIP, _send_trip, SEND_TRIP_SCHEMA),
    ):
        hass.services.async_register(DOMAIN, service, handler, schema=schema)
