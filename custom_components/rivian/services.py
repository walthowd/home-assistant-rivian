"""Rivian actions."""

from __future__ import annotations

from rivian.exceptions import RivianApiException
from rivian.rivian import CHARGING_NETWORKS
from rivian.utils import place_url
import voluptuous as vol

from homeassistant.const import ATTR_DEVICE_ID, ATTR_LATITUDE, ATTR_LONGITUDE
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr

from .const import ATTR_COORDINATOR, ATTR_USER, ATTR_VEHICLE, DOMAIN
from .coordinator import VehicleCoordinator

SERVICE_END_TRIP = "end_trip"
SERVICE_SEARCH_PLACES = "search_places"
SERVICE_SEND_LOCATION = "send_location"
SERVICE_SEND_TRIP = "send_trip"

SEND_LOCATION_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_LATITUDE): cv.latitude,
        vol.Required(ATTR_LONGITUDE): cv.longitude,
        vol.Inclusive("place_id", "place"): cv.string,
        vol.Inclusive("name", "place"): cv.string,
        vol.Optional("address"): cv.string,
    }
)

SEARCH_PLACES_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required("query"): cv.string,
        vol.Inclusive(ATTR_LATITUDE, "near"): cv.latitude,
        vol.Inclusive(ATTR_LONGITUDE, "near"): cv.longitude,
        vol.Optional("max_results", default=8): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=20)
        ),
        vol.Optional("autocomplete", default=False): cv.boolean,
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
    if place_id := call.data.get("place_id"):
        url = place_url(
            place_id, call.data["name"], latitude, longitude, call.data.get("address")
        )
    else:
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


def _position(coor: VehicleCoordinator) -> tuple[float, float]:
    """Return the vehicle's latitude and longitude."""
    location = (coor.data or {}).get("gnssLocation") or {}
    latitude, longitude = location.get("latitude"), location.get("longitude")
    if latitude is None or longitude is None:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="location_unknown"
        )
    return latitude, longitude


async def _search_places(call: ServiceCall) -> ServiceResponse:
    """Search for places, or suggest searches, from the vehicle or coordinates."""
    coor = _vehicle(call.hass, call.data[ATTR_DEVICE_ID])
    if ATTR_LATITUDE in call.data:
        near = (call.data[ATTR_LATITUDE], call.data[ATTR_LONGITUDE])
    else:
        near = _position(coor)
    try:
        if call.data["autocomplete"]:
            predictions = await coor.api.suggest_places(call.data["query"], *near)
            return {"predictions": predictions, "results": []}
        places = await coor.api.search_places(call.data["query"], *near)
    except RivianApiException as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="search_failed",
            translation_placeholders={"error": str(err)},
        ) from err
    results = []
    for place in places[: call.data["max_results"]]:
        location = place.get("location") or {}
        point = location.get("navCoordinate") or location.get("coordinate") or {}
        drive = place.get("driveDetails") or {}
        results.append(
            {
                "place_id": place["id"],
                "name": place["name"],
                "address": location.get("formattedAddress"),
                "latitude": point.get("latitude"),
                "longitude": point.get("longitude"),
                "drive_distance": drive.get("driveDistance"),
                "drive_duration": drive.get("driveDuration"),
            }
        )
    return {"predictions": [], "results": results}


async def _send_trip(call: ServiceCall) -> None:
    """Send stops to the vehicle, which plans the route and charging stops."""
    coor = _vehicle(call.hass, call.data[ATTR_DEVICE_ID])
    origin = _position(coor)
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
    for service, handler, schema, response in (
        (SERVICE_END_TRIP, _end_trip, END_TRIP_SCHEMA, SupportsResponse.NONE),
        (
            SERVICE_SEARCH_PLACES,
            _search_places,
            SEARCH_PLACES_SCHEMA,
            SupportsResponse.ONLY,
        ),
        (
            SERVICE_SEND_LOCATION,
            _send_location,
            SEND_LOCATION_SCHEMA,
            SupportsResponse.NONE,
        ),
        (SERVICE_SEND_TRIP, _send_trip, SEND_TRIP_SCHEMA, SupportsResponse.NONE),
    ):
        hass.services.async_register(
            DOMAIN, service, handler, schema=schema, supports_response=response
        )
