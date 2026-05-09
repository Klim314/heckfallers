"""Match-event emission helper.

Events are append-only dicts on ``world.match_events``, capped per tick
in ``World.step``. Each event carries a ``type`` discriminator, the
emitting ``tick``, and arbitrary type-specific payload keys. The wire
format is sent to the client every tick (see ``serialize.world_to_wire``).

Event types currently emitted:

- ``cell_captured``: defender flip. ``coord``, ``defender``, ``breakthrough``.
- ``cell_repulsed``: contested cell pushed back to uncontested. ``coord``,
  ``defender`` (the side that held).
- ``salient_spawned``: enemy salient created. ``salient_id``, ``kind``
  (destroy|conquer), ``target`` (or null), ``target_poi_id`` (or null).
- ``salient_ended``: salient removed. ``salient_id``, ``kind``, ``reason``
  (success|expired). Replaces the older ``destroy_salient_success``.
- ``build_started``: SE build site placed. ``coord``, ``target_kind``,
  ``owner``, ``completes_at``.
- ``build_completed``: build site resolved into final POI. ``coord``,
  ``kind``, ``owner``.
- ``poi_placed``: any direct POI placement (factory, resistance node,
  player-placed FOB/arty/etc.). ``poi_id``, ``kind``, ``owner``, ``coord``.
- ``factory_strike``: factory began pushing into a fresh SE cell.
  ``coord``, ``factory_id``.
- ``match_ended``: match transitioned to a terminal state. ``winner``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .world import World


def emit(world: "World", type: str, **payload) -> None:
    world.match_events.append({"type": type, "tick": world.tick, **payload})
