"""Rivian actions."""

from __future__ import annotations

from rivian.exceptions import RivianApiException
import voluptuous as vol

from homeassistant.const import ATTR_DEVICE_ID, ATTR_LATITUDE, ATTR_LONGITUDE
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr

from .const import ATTR_COORDINATOR, ATTR_VEHICLE, DOMAIN
from .coordinator import VehicleCoordinator

SERVICE_SEND_LOCATION = "send_location"

SEND_LOCATION_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_LATITUDE): cv.latitude,
        vol.Required(ATTR_LONGITUDE): cv.longitude,
    }
)


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


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the Rivian actions."""
    hass.services.async_register(
        DOMAIN, SERVICE_SEND_LOCATION, _send_location, schema=SEND_LOCATION_SCHEMA
    )
