"""Entities and Devices queries for logbook."""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import lambda_stmt, select, union_all
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.lambdas import StatementLambdaElement
from sqlalchemy.sql.selectable import CTE, CompoundSelect, Select

from homeassistant.components.recorder.db_schema import EventData, Events, States

from .common import (
    apply_events_context_hints,
    apply_states_context_hints,
    select_events_context_id_subquery,
    select_events_context_only,
    select_events_without_states,
    select_states_context_only,
)
from .devices import apply_event_device_id_matchers
from .entities import (
    apply_entities_hints,
    apply_event_entity_id_matchers,
    states_select_for_entity_ids,
)


def _select_entities_device_id_context_ids_sub_query(
    start_day: float,
    end_day: float,
    event_types: tuple[str, ...],
    entity_ids: list[str],
    json_quoted_entity_ids: list[str],
    json_quoted_device_ids: list[str],
) -> Select:
    """Generate a subquery to find context ids for multiple entities and multiple devices."""
    union = union_all(
        select_events_context_id_subquery(start_day, end_day, event_types).where(
            _apply_event_entity_id_device_id_matchers(
                json_quoted_entity_ids, json_quoted_device_ids
            )
        ),
        apply_entities_hints(select(States.context_id_bin))
        .filter(
            (States.last_updated_ts > start_day) & (States.last_updated_ts < end_day)
        )
        .where(States.entity_id.in_(entity_ids)),
    )
    return select(union.c.context_id_bin).group_by(union.c.context_id_bin)


def _apply_entities_devices_context_union(
    sel: Select,
    start_day: float,
    end_day: float,
    event_types: tuple[str, ...],
    entity_ids: list[str],
    json_quoted_entity_ids: list[str],
    json_quoted_device_ids: list[str],
) -> CompoundSelect:
    devices_entities_cte: CTE = _select_entities_device_id_context_ids_sub_query(
        start_day,
        end_day,
        event_types,
        entity_ids,
        json_quoted_entity_ids,
        json_quoted_device_ids,
    ).cte()
    # We used to optimize this to exclude rows we already in the union with
    # a States.entity_id.not_in(entity_ids) but that made the
    # query much slower on MySQL, and since we already filter them away
    # in the python code anyways since they will have context_only
    # set on them the impact is minimal.
    return sel.union_all(
        states_select_for_entity_ids(start_day, end_day, entity_ids),
        apply_events_context_hints(
            select_events_context_only()
            .select_from(devices_entities_cte)
            .outerjoin(
                Events, devices_entities_cte.c.context_id_bin == Events.context_id_bin
            )
        ).outerjoin(EventData, (Events.data_id == EventData.data_id)),
        apply_states_context_hints(
            select_states_context_only()
            .select_from(devices_entities_cte)
            .outerjoin(
                States, devices_entities_cte.c.context_id_bin == States.context_id_bin
            )
        ),
    )


def entities_devices_stmt(
    start_day: float,
    end_day: float,
    event_types: tuple[str, ...],
    entity_ids: list[str],
    json_quoted_entity_ids: list[str],
    json_quoted_device_ids: list[str],
) -> StatementLambdaElement:
    """Generate a logbook query for multiple entities."""
    stmt = lambda_stmt(
        lambda: _apply_entities_devices_context_union(
            select_events_without_states(start_day, end_day, event_types).where(
                _apply_event_entity_id_device_id_matchers(
                    json_quoted_entity_ids, json_quoted_device_ids
                )
            ),
            start_day,
            end_day,
            event_types,
            entity_ids,
            json_quoted_entity_ids,
            json_quoted_device_ids,
        ).order_by(Events.time_fired_ts)
    )
    return stmt


def _apply_event_entity_id_device_id_matchers(
    json_quoted_entity_ids: Iterable[str], json_quoted_device_ids: Iterable[str]
) -> ColumnElement[bool]:
    """Create matchers for the device_id and entity_id in the event_data."""
    return apply_event_entity_id_matchers(
        json_quoted_entity_ids
    ) | apply_event_device_id_matchers(json_quoted_device_ids)
