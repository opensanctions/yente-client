"""Project response entities into the views the MCP tools return.

yente entities are large — every property, referents, nested provenance.
Handing that raw to a model buries the signal, so every tool projects entities
through one function, :func:`shape`, at one of three levels:

- ``ref``: identity, topics and counts — ``property_count`` and
  ``value_count`` for what the full record holds, ``dataset_count`` for how
  many sources list it. Every entity an agent would look up next: a match
  candidate (which adds the match evidence), a search result, and the far end
  of a relationship (an owner, a relative).
- ``stub``: identity, topics and the schema's FtM ``featured`` properties. The
  post of a position held, which is part of the position rather than a
  neighbour, and the designating record of a description.
- ``summary``: datasets and every short-valued property. Relation edges and
  the record in a profile. Only a top-level record carries ``first_seen`` /
  ``last_change``; on edges the dates are noise.

``ref`` and ``stub`` carry ``abridged``, naming the ``fetch_entity_by_id`` call
that returns the full record, so a missing field reads as "not shown" rather
than "not known".

Prose — properties of the FtM ``text`` type, such as ``notes`` or a sanction's
``reason`` — never appears in these views. It is large, and it needs per-value
credits a merged entity can't give, so it has its own tool, built by
:func:`text_entries` from the statement store.

Topic labels sit inline on each entity: they're the risk signal, and a label
next to its code gets used where a separate legend gets skipped. Every other
code resolves through the response's ``legend`` (:mod:`~yente_client.mcp.legend`).

Pure functions over the SDK response models — no FastMCP, unit-testable.
"""

import difflib
import functools
import json
from collections.abc import Callable, Mapping
from collections.abc import Set as AbstractSet
from datetime import datetime
from typing import Any, Literal

from yente_client import schemas
from yente_client.models import (
    AdjacentPropertyResponse,
    AdjacentResponse,
    Entity,
    FeatureResult,
    ScoredEntity,
    Statement,
    StatementsResponse,
    TotalSpec,
)
from yente_client.schemas import (
    property_specs,
    schema_spec,
    type_spec,
)

Level = Literal["ref", "stub", "summary"]


# Value types left out of every view. `text` / `html` are the prose served by
# the descriptions tool; the rest is machine payload.
_PROSE_TYPES: frozenset[str] = frozenset({"text", "html"})
_BULK_TYPES: frozenset[str] = _PROSE_TYPES | {"json", "checksum", "mimetype"}

# How many catalog entries an unknown-name error suggests.
_SUGGESTIONS = 5

# How many positive scoring features to surface per candidate. Negative
# features are always kept: penalties are what explain a near-miss.
_EXPLANATION_LIMIT = 3

# Relation props suppressed from the adjacency projection. `riskSource` is a
# `Thing` entity property with no reverse: the adjacency endpoint reports a count
# for it but resolves no counterparties — a phantom edge. An entity's risk basis
# is read from its own record (topics / properties), not traversed as a relation.
HIDDEN_RELATION_PROPS: frozenset[str] = frozenset({"riskSource"})

# Relations that carry an entity's risk basis: shown in full at the top level of
# a profile rather than capped under `relations`. A Sanction imposes a
# restriction; a Risk names the entity without one (watch lists, advisories).
PROFILE_SECTIONS: tuple[str, ...] = ("sanctions", "risks", "positionOccupancies")

# The relations whose records carry designation prose (a sanction's or risk's
# `reason`), in the order the descriptions tool lists them.
DESIGNATION_SECTIONS: tuple[str, ...] = ("sanctions", "risks")


def shape(
    entity: Entity,
    level: Level = "summary",
    *,
    source_id: str | None = None,
    nested: Level = "stub",
) -> dict[str, Any]:
    """Project an entity at the given level.

    Args:
        source_id: The entity this one was reached from, when it is a relation
            edge. Its back-reference to the source is dropped as redundant, and
            so are its dates.
        nested: The level for entities nested in this one's properties.
    """
    shaped: dict[str, Any] = {
        "id": entity.id,
        "caption": entity.caption,
        "schema": entity.schema_,
    }
    topics = _topic_labels(entity)
    if topics:
        shaped["topics"] = topics
    if level == "ref":
        values = [v for v in _summary_values(entity).values()]
        shaped["property_count"] = len(values)
        shaped["value_count"] = sum(len(v) for v in values)
        shaped["dataset_count"] = len(entity.datasets)
        shaped["abridged"] = tool_call("fetch_entity_by_id", entity_id=entity.id)
        return shaped
    if level == "stub":
        props = _stub_properties(entity)
        if props:
            shaped["properties"] = props
        shaped["abridged"] = tool_call("fetch_entity_by_id", entity_id=entity.id)
        return shaped
    if entity.datasets:
        shaped["datasets"] = entity.datasets
    shaped["properties"] = _properties(entity, source_id, nested)
    if source_id is None:
        for key in ("first_seen", "last_change"):
            value = _date(getattr(entity, key))
            if value is not None:
                shaped[key] = value
    return shaped


def shape_scored(entity: ScoredEntity) -> dict[str, Any]:
    """Project a match candidate: a ``ref`` plus the match evidence."""
    shaped = shape(entity, "ref")
    shaped["score"] = entity.score
    shaped["match"] = entity.match
    shaped["explanation"] = _explanation(entity)
    return shaped


def shape_raw(entity: Entity) -> dict[str, Any]:
    """Return the untrimmed model dump, with inline topic labels."""
    record = entity.model_dump(by_alias=True, mode="json", exclude_none=True)
    topics = _topic_labels(entity)
    if topics:
        record["topics"] = topics
    return record


def shape_profile(entity: Entity, adjacency: AdjacentResponse) -> dict[str, Any]:
    """Assemble the one-call entity profile: record, risk basis, relation previews.

    Each of :data:`PROFILE_SECTIONS` present on the entity moves from the
    relations previews to the top level. ``narrative_text`` names the call for
    the record's prose and its designation reasons, when it has either.
    """
    profile = shape(entity)
    relations = shape_adjacency(adjacency)
    for prop in PROFILE_SECTIONS:
        section = relations.pop(prop, None)
        if section is not None:
            profile[prop] = section
    if relations:
        profile["relations"] = relations
    designated = any(
        (block := adjacency.adjacent.get(prop)) is not None and block.total.value
        for prop in DESIGNATION_SECTIONS
    )
    if designated or _prose_values(entity):
        profile["narrative_text"] = tool_call("fetch_entity_descriptions", entity_id=entity.id)
    return profile


def tool_call(name: str, **args: Any) -> str:
    """Spell out an MCP tool call, e.g. ``fetch_entity_by_id(entity_id="Q7747")``.

    The form every ``abridged`` / ``next`` / ``narrative_text`` pointer takes, so
    an agent can follow one without working out the tool or its arguments.
    Arguments passed as ``None`` are left out.
    """
    parts = ", ".join(
        f"{key}={json.dumps(value, ensure_ascii=False)}"
        for key, value in args.items()
        if value is not None
    )
    return f"{name}({parts})"


def page_info(
    total: TotalSpec, offset: int, returned: int, next_call: Callable[[int], str]
) -> dict[str, Any]:
    """Build the paging envelope every list carries.

    ``next`` is the call for the following page, built by ``next_call`` from
    its offset, and ``None`` exactly when the list is complete, so a model
    can't mistake a first page for the whole set.
    """
    end = offset + returned
    return {
        "total": total.value,
        "returned": returned,
        "next": next_call(end) if end < total.value else None,
    }


def shape_relation_page(
    block: AdjacentPropertyResponse, source_id: str, prop: str, *, limit: int | None = None
) -> dict[str, Any]:
    """Shape one edge type's page: paging envelope plus edges.

    ``next`` pages on with ``fetch_entity_relations``, at ``limit`` if the
    caller chose one, else at that tool's default page size.

    The far end of an ordinary relationship is a ``ref``; in a profile section
    (:data:`PROFILE_SECTIONS`) it is a ``stub``, since a position's post is
    part of the position held. Designation rows drop their caption, which
    repeats the program.
    """
    nested: Level = "stub" if prop in PROFILE_SECTIONS else "ref"
    results: list[dict[str, Any]] = []
    for edge in block.results:
        if not isinstance(edge, Entity):
            results.append({"id": edge})
            continue
        row = shape(edge, source_id=source_id, nested=nested)
        if prop in DESIGNATION_SECTIONS:
            row.pop("caption")
        results.append(row)

    def next_call(offset: int) -> str:
        return tool_call(
            "fetch_entity_relations", entity_id=source_id, prop=prop, limit=limit, offset=offset
        )

    return {**page_info(block.total, block.offset, len(results), next_call), "results": results}


def shape_adjacency(resp: AdjacentResponse) -> dict[str, Any]:
    """Shape the all-edge-types overview: edge type → paged edges."""
    source_id = resp.entity.id
    return {
        prop: shape_relation_page(block, source_id, prop)
        for prop, block in resp.adjacent.items()
        if prop not in HIDDEN_RELATION_PROPS
    }


def shape_statement(stmt: Statement) -> dict[str, Any]:
    """Project one statement row to its provenance essentials.

    ``entity_id`` is the pre-deduplication source record that asserted the
    claim; the canonical ID is dropped per row (it's the query).
    ``original_value`` only appears when cleaning changed it, ``origin`` when
    the claim wasn't plain crawled data (e.g. ``"inferred"``, ``"patch"``).
    """
    rec: dict[str, Any] = {
        "prop": stmt.prop,
        "value": stmt.value,
        "dataset": stmt.dataset,
        "entity_id": stmt.entity_id,
        "first_seen": _date(stmt.first_seen),
        "last_seen": _date(stmt.last_seen),
    }
    if stmt.lang:
        rec["lang"] = stmt.lang
    if stmt.original_value and stmt.original_value != stmt.value:
        rec["original_value"] = stmt.original_value
    if stmt.origin:
        rec["origin"] = stmt.origin
    return rec


def shape_statements(resp: StatementsResponse, next_call: Callable[[int], str]) -> dict[str, Any]:
    """Shape a statements page: paging envelope plus projected rows."""
    results = [shape_statement(s) for s in resp.results]
    return {**page_info(resp.total, resp.offset, len(results), next_call), "results": results}


def text_property_names(schema: str) -> tuple[str, ...]:
    """The prose properties of ``schema`` worth reading, e.g. ``notes``, ``reason``."""
    return _prose_names(schema)


def designation_props(schema: str) -> tuple[str, ...]:
    """The :data:`DESIGNATION_SECTIONS` relations ``schema`` actually has."""
    allowed = _summary_names(schema)
    return tuple(p for p in DESIGNATION_SECTIONS if p in allowed)


def text_entries(
    entity: Entity,
    statements: list[Statement] | None,
    designations: Mapping[str, list[Entity]],
    unofficial: AbstractSet[str],
) -> list[dict[str, Any]]:
    """Build the credited prose entries for the descriptions tool, in reading order.

    Designation prose comes first (sanctions, then risks), each entry carrying
    the record it belongs to; the entity's own prose follows, credited per value
    from ``statements``. Within each group, entries backed by an official
    source precede those from unofficial ones (aggregators, NGOs, Wikidata).
    Identical texts collapse into one entry with every source; translations
    stay separate. With ``statements`` of ``None`` (no statement store), the
    entity's own prose comes from its merged record, without credits.

    Args:
        designations: Sanction / Risk records by section name, as in
            :data:`DESIGNATION_SECTIONS`.
        unofficial: Dataset names whose publisher isn't official.
    """
    groups: list[list[dict[str, Any]]] = []
    for prop in DESIGNATION_SECTIONS:
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for record in designations.get(prop, []):
            sources = [
                {"dataset": d, "first_seen": _date(record.first_seen)} for d in record.datasets
            ]
            for name, values in _prose_values(record).items():
                for value in values:
                    entry = merged.setdefault(
                        (name, value), {"prop": name, "text": value, "records": [], "sources": []}
                    )
                    entry["records"].append(shape(record, "stub"))
                    entry["sources"].extend(sources)
        groups.append(list(merged.values()))
    own: dict[tuple[str, str], dict[str, Any]] = {}
    if statements is None:
        for name, values in _prose_values(entity).items():
            for value in values:
                own.setdefault((name, value), {"prop": name, "text": value, "sources": []})
    else:
        for stmt in statements:
            entry = own.setdefault(
                (stmt.prop, stmt.value), {"prop": stmt.prop, "text": stmt.value, "sources": []}
            )
            if stmt.lang and "lang" not in entry:
                entry["lang"] = stmt.lang
            entry["sources"].append({"dataset": stmt.dataset, "first_seen": _date(stmt.first_seen)})
    groups.append(list(own.values()))
    ordered: list[dict[str, Any]] = []
    for group in groups:
        for entry in group:
            entry["sources"] = _order_sources(entry["sources"], unofficial)
        group.sort(key=lambda e: _entry_rank(e, unofficial))
        ordered.extend(group)
    return ordered


def page_entries(
    entries: list[dict[str, Any]], offset: int, budget: int, next_call: Callable[[int], str]
) -> dict[str, Any]:
    """Page ``entries`` from ``offset`` until about ``budget`` characters of JSON.

    A page always holds at least one entry, however long, so paging can't stall.
    """
    page: list[dict[str, Any]] = []
    used = 0
    for entry in entries[offset:]:
        size = len(json.dumps(entry, ensure_ascii=False))
        if page and used + size > budget:
            break
        page.append(entry)
        used += size
    total = TotalSpec(value=len(entries), relation="eq")
    return {**page_info(total, offset, len(page), next_call), "results": page}


def unknown_name(kind: str, name: str, titles: Mapping[str, str | None]) -> str:
    """Build the error for an unknown catalog name, listing the closest entries.

    The catalogs are too large to list whole, so a miss is the agent's way in:
    entries whose name or title contains ``name`` come first, then fuzzy
    matches on either.

    Args:
        titles: Every catalog name mapped to its title.
    """
    needle = name.lower()
    by_text = {key.lower(): key for key in titles}
    by_text.update({t.lower(): key for key, t in titles.items() if t})
    contained = [key for text, key in by_text.items() if needle in text]
    close = [by_text[t] for t in difflib.get_close_matches(needle, by_text, n=_SUGGESTIONS)]
    hits = list(dict.fromkeys(contained + close))[:_SUGGESTIONS]
    if not hits:
        return f"Unknown {kind} {name!r}, and nothing close to it."
    listed = "; ".join(f"{key} ({titles[key]})" if titles[key] else key for key in hits)
    return f"Unknown {kind} {name!r}. Closest: {listed}."


def _properties(entity: Entity, source_id: str | None, nested: Level) -> dict[str, list[Any]]:
    """Pick the entity's summary properties; nested entities are shaped at ``nested``."""
    allowed = _summary_names(entity.schema_)
    out: dict[str, list[Any]] = {}
    for name, values in entity.properties.items():
        if name not in allowed:
            continue
        kept: list[Any] = []
        for value in values:
            if isinstance(value, Entity):
                kept.append(shape(value, nested))
            elif value != source_id:
                kept.append(value)
        if kept:
            out[name] = kept
    return out


def _stub_properties(entity: Entity) -> dict[str, list[str]]:
    """Pick the entity's featured, non-entity properties as plain strings.

    A ``name`` that only repeats the caption is left out.
    """
    out: dict[str, list[str]] = {}
    for name in _featured_names(entity.schema_):
        values = entity.get(name)
        if name == "name" and values == [entity.caption]:
            continue
        if values:
            out[name] = values
    return out


def _summary_values(entity: Entity) -> dict[str, list[Any]]:
    """The entity's non-empty values for the properties a summary would show."""
    allowed = _summary_names(entity.schema_)
    return {
        name: values for name, values in entity.properties.items() if values and name in allowed
    }


def _prose_values(entity: Entity) -> dict[str, list[str]]:
    """The entity's prose property values, by property name."""
    out: dict[str, list[str]] = {}
    for name in _prose_names(entity.schema_):
        values = entity.get(name)
        if values:
            out[name] = values
    return out


def _order_sources(
    sources: list[dict[str, Any]], unofficial: AbstractSet[str]
) -> list[dict[str, Any]]:
    """Deduplicate sources by dataset (earliest first_seen) and put official ones first."""
    earliest: dict[str, dict[str, Any]] = {}
    for src in sources:
        seen = earliest.get(src["dataset"])
        if seen is None or (src["first_seen"] or "") < (seen["first_seen"] or ""):
            earliest[src["dataset"]] = src
    return sorted(earliest.values(), key=lambda s: (s["dataset"] in unofficial, s["dataset"]))


def _entry_rank(entry: dict[str, Any], unofficial: AbstractSet[str]) -> tuple[bool, bool, str]:
    """Sort key: official-backed entries first, a designation's `reason` ahead of
    its other prose (often a legal-instrument citation), then by dataset name."""
    sources = entry["sources"]
    official = any(s["dataset"] not in unofficial for s in sources)
    return (not official, entry["prop"] != "reason", sources[0]["dataset"] if sources else "")


@functools.cache
def _prose_names(schema: str) -> tuple[str, ...]:
    """``schema``'s visible prose properties (text-typed, not hidden), sorted."""
    if not schemas.has_schema(schema):
        return ()
    return tuple(
        sorted(
            name
            for name, prop in property_specs(schema).items()
            if prop.type in _PROSE_TYPES and not prop.hidden
        )
    )


@functools.cache
def _featured_names(schema: str) -> tuple[str, ...]:
    """``schema``'s FtM featured properties, minus entity- and topic-typed ones."""
    if not schemas.has_schema(schema):
        return ()
    allowed = _summary_names(schema)
    specs = property_specs(schema)
    return tuple(
        name
        for name in schema_spec(schema).featured
        if name in allowed and specs[name].type != "entity"
    )


@functools.cache
def _summary_names(schema: str) -> frozenset[str]:
    """Names of ``schema``'s properties a summary shows (empty for unknown schemata).

    Stubs and briefs narrow this to the featured names.
    """
    if not schemas.has_schema(schema):
        return frozenset()
    excluded = _BULK_TYPES | {"topic"}
    return frozenset(
        name for name, prop in property_specs(schema).items() if prop.type not in excluded
    )


def _topic_labels(entity: Entity) -> dict[str, str]:
    """Resolve the entity's topic codes to a ``code → label`` map.

    A code missing from the bundled vocabulary (model drift) falls back to
    itself rather than being dropped — a raw code is still signal.
    """
    vocab = type_spec("topic").values
    return {code: vocab.get(code, code) for code in entity.get("topics")}


def _explanation(entity: ScoredEntity) -> dict[str, dict[str, Any]]:
    """Return the features that explain the score, with the values they compared.

    The strongest positive features plus every negative one, ranked by
    absolute score. ``detail`` / ``query`` / ``candidate`` carry the evidence
    (e.g. which two birth dates disagreed) and appear only when the matcher
    supplied them.
    """
    features = entity.contributing_explanations.items()
    positive = sorted((kv for kv in features if kv[1].score > 0), key=lambda kv: -kv[1].score)
    negative = [kv for kv in features if kv[1].score < 0]
    ranked = sorted(
        positive[:_EXPLANATION_LIMIT] + negative, key=lambda kv: abs(kv[1].score), reverse=True
    )
    return {name: _feature(feature) for name, feature in ranked}


def _feature(feature: FeatureResult) -> dict[str, Any]:
    rec: dict[str, Any] = {"score": feature.score}
    for key in ("detail", "query", "candidate"):
        value = getattr(feature, key)
        if value:
            rec[key] = value
    return rec


def _date(value: datetime | None) -> str | None:
    return value.date().isoformat() if value is not None else None
