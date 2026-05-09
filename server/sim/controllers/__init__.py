"""Enemy controller protocol — strategies that drive enemy decisions.

A controller is the *who/when/why* layer over the salient primitive. It
owns its own cadence and state (cooldowns, last-spawn ticks, scoring
caches). The world tick calls ``controller.tick(world)`` once per step;
the controller itself decides what to do based on ``world.tick``.

v1 ships ``OpportunisticController`` only. New variants (aggressive,
defensive, scripted) plug in by implementing the protocol.
"""
from __future__ import annotations

from typing import Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from ..world import World


class EnemyController(Protocol):
    name: str

    def tick(self, world: "World") -> None:
        """Called every world step. Implementations gate their own cadence."""
        ...


from .high_command import HighCommandController  # noqa: E402
from .opportunistic import OpportunisticController  # noqa: E402

__all__ = ["EnemyController", "HighCommandController", "OpportunisticController"]
