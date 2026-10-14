"""The live fixture may recapture rejected startup input, never replay uncertainty."""

import runpy
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

Smoke = runpy.run_path(str(Path(__file__).parents[2] / "scripts" / "desktop_smoke.py"))["Smoke"]


async def test_startup_key_recaptures_after_effect_none_stale_response():
    fixture = object.__new__(Smoke)
    fixture.observe = AsyncMock(side_effect=[{"observation_id": "old"}, {"observation_id": "new"}])
    fixture.act = AsyncMock(
        side_effect=[
            {"error": {"code": "stale_observation", "effect": "none"}},
            {"content": []},
        ]
    )
    await fixture.startup_key("Escape")
    assert fixture.observe.await_count == 2
    assert fixture.act.await_args_list[1].args[0]["observation_id"] == "new"


@pytest.mark.parametrize(
    "error",
    [
        {"code": "action_effect_unknown", "effect": "unknown"},
        {"code": "capability_denied", "effect": "none"},
    ],
)
async def test_startup_key_never_replays_uncertainty_or_nonstale_failures(error):
    fixture = object.__new__(Smoke)
    fixture.observe = AsyncMock(return_value={"observation_id": "once"})
    fixture.act = AsyncMock(return_value={"error": error})
    with pytest.raises(AssertionError):
        await fixture.startup_key("Escape")
    assert fixture.act.await_count == 1
