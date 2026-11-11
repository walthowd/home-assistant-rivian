"""Tests for the cabin climate entity."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call, patch

from custom_components.rivian import climate


def entity(values: dict[str, object]) -> climate.RivianClimateEntity:
    """A climate entity reading the given vehicle values."""
    climate_entity = object.__new__(climate.RivianClimateEntity)
    climate_entity._get_value = MagicMock(side_effect=values.get)  # type: ignore[method-assign]
    return climate_entity


def test_native_temperatures() -> None:
    """Temperatures are the vehicle's, in °C."""
    cabin = entity(
        {"cabinClimateInteriorTemperature": 18.5, "hvacTargetTemperature": 21}
    )
    assert cabin.native_current_temperature == 18.5
    assert cabin.native_target_temperature == 21
    # The test harness's ClimateEntity stands in for Home Assistant before 2026.11
    assert (cabin.current_temperature, cabin.target_temperature) == (18.5, 21)


def test_lo_hi_presets_read_celsius() -> None:
    """LO and HI are the vehicle's 0 °C and 63.5 °C set points."""
    for target, preset in ((0, "LO"), (63.5, "HI"), (21, None)):
        cabin = entity({"defrostDefogStatus": "off", "hvacTargetTemperature": target})
        assert cabin.preset_mode == preset


def test_on_during_pet_mode_and_climate_hold() -> None:
    """Pet mode and climate hold show as on, as the vehicle reports no precondition."""
    off = {"defrostDefogStatus": "off", "cabinPreconditioningType": "none"}
    assert entity(off).hvac_mode == climate.HVACMode.OFF
    assert entity(off | {"petModeStatus": "on"}).hvac_mode == climate.HVACMode.HEAT_COOL
    assert (
        entity(off | {"climateHoldStatus": "on"}).hvac_mode
        == climate.HVACMode.HEAT_COOL
    )


async def test_off_ends_pet_mode_and_climate_hold() -> None:
    """Turning off ends pet mode and climate hold, which preconditioning doesn't."""
    cabin = entity({"petModeStatus": "on", "climateHoldStatus": "on"})
    cabin.coordinator = MagicMock(
        send_parallax_command=AsyncMock(), send_vehicle_command=AsyncMock()
    )
    with (
        patch.object(climate, "VehicleCommand"),
        patch.object(climate, "encode_pet_mode", lambda on: ("pet", on)),
        patch.object(climate, "encode_climate_hold", lambda on: ("hold", on)),
    ):
        await cabin.async_set_hvac_mode(climate.HVACMode.OFF)
    assert cabin.coordinator.send_parallax_command.await_args_list == [
        call(climate.SET_PET_MODE_RVM, ("pet", False)),
        call(climate.SET_CLIMATE_HOLD_RVM, ("hold", False)),
    ]
    cabin.coordinator.send_vehicle_command.assert_awaited_once()
