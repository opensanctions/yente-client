"""Project response entities into the views the MCP tools return.

yente entities are large — every property, referents, nested provenance.
Handing that raw to a model buries the signal, so every tool projects entities
through one function, :func:`shape`, at one of three levels:

- ``ref``: identity plus topics. The far end of a relationship (an owner, a
  relative), so a sanctioned neighbour is visible; its record is one
  ``fetch_entity_by_id`` call away.
- ``stub``: ref plus the schema's FtM ``featured`` properties. The post of a
  position held, which is part of the position rather than a neighbour, and
  the designating record of a description.
- ``summary``: stub plus datasets and every short-valued property. Match and
  search results, relation edges, and the record in a profile. Only a
  top-level record carries ``first_seen`` / ``last_change``; on edges the
  dates are noise.

``ref`` and ``stub`` carry ``abridged: true``, so a missing field reads as
"not shown" rather than "not known".

Prose — properties of the FtM ``text`` type, such as ``notes`` or a sanction's
``reason`` — never appears in these views. It is large, and it needs per-value
credits a merged entity can't give, so it has its own tool, built by
:func:`text_entries` from the statement store.

Topic labels sit inline on each entity: they're the risk signal, and a label
next to its code gets used where a separate legend gets skipped. Every other
code — schemata, property names, enum values such as countries and languages,
dataset and program keys, match features — resolves through one ``legend``
per response, built by walking the finished output (:func:`collect_codes`,
:func:`build_legend`), so it describes only codes the response contains. The
legend also says how to link to an entity (``links``).

Pure functions over the SDK response models — no FastMCP, unit-testable.
"""

import difflib
import functools
import json
from collections.abc import Iterable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from yente_client import schemas
from yente_client.models import (
    AdjacentPropertyResponse,
    AdjacentResponse,
    AlgorithmsResponse,
    DatasetsResponse,
    Entity,
    FeatureResult,
    Program,
    ScoredEntity,
    Statement,
    StatementsResponse,
    TotalSpec,
)
from yente_client.schemas import model

Level = Literal["ref", "stub", "summary"]

# A property's (label, type, range); range is the target schema of an entity-typed one.
_PropDef = tuple[str | None, str | None, str | None]

WEBSITE = "https://www.opensanctions.org"

# The collection whose index build a screen reports as its `as_of`.
DEFAULT_COLLECTION = "default"

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
        shaped["abridged"] = True
        return shaped
    if level == "stub":
        props = _stub_properties(entity)
        if props:
            shaped["properties"] = props
        shaped["abridged"] = True
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
    """Project a match candidate: summary plus score, match flag and explanation."""
    shaped = shape(entity)
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


def shape_profile(
    entity: Entity,
    adjacency: AdjacentResponse,
    full_pages: Mapping[str, AdjacentPropertyResponse] | None = None,
) -> dict[str, Any]:
    """Assemble the one-call entity profile: record, risk basis, relation summary.

    Each of :data:`PROFILE_SECTIONS` present on the entity moves from the
    relations overview to the top level. ``full_pages`` holds a full page for
    any section whose overview page was capped. ``descriptions`` counts the
    prose values left out, per property, on the record and its designations.
    """
    full_pages = full_pages or {}
    profile = shape(entity)
    relations = shape_adjacency(adjacency)
    for prop in PROFILE_SECTIONS:
        first_page = relations.pop(prop, None)
        if prop in full_pages:
            profile[prop] = shape_relation_page(full_pages[prop], entity.id, prop)
        elif first_page is not None:
            profile[prop] = first_page
    if relations:
        profile["relations"] = relations
    designations = [
        edge
        for prop in DESIGNATION_SECTIONS
        if (block := full_pages.get(prop) or adjacency.adjacent.get(prop)) is not None
        for edge in block.results
        if isinstance(edge, Entity)
    ]
    counts: dict[str, int] = {}
    for item in [entity, *designations]:
        for name, values in _prose_values(item).items():
            counts[name] = counts.get(name, 0) + len(values)
    if counts:
        profile["descriptions"] = counts
    return profile


def page_info(total: TotalSpec, offset: int, returned: int) -> dict[str, Any]:
    """Build the paging envelope every list carries.

    ``next_offset`` is ``None`` exactly when the list is complete, so a model
    can't mistake a first page for the whole set.
    """
    end = offset + returned
    return {
        "total": total.value,
        "returned": returned,
        "offset": offset,
        "next_offset": end if end < total.value else None,
    }


def shape_relation_page(
    block: AdjacentPropertyResponse, source_id: str, prop: str
) -> dict[str, Any]:
    """Shape one edge type's page: paging envelope plus edges.

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
    return {**page_info(block.total, block.offset, len(results)), "results": results}


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
        "first_seen": stmt.first_seen.date().isoformat(),
        "last_seen": stmt.last_seen.date().isoformat(),
    }
    if stmt.lang:
        rec["lang"] = stmt.lang
    if stmt.original_value and stmt.original_value != stmt.value:
        rec["original_value"] = stmt.original_value
    if stmt.origin:
        rec["origin"] = stmt.origin
    return rec


def shape_statements(resp: StatementsResponse) -> dict[str, Any]:
    """Shape a statements page: paging envelope plus projected rows."""
    results = [shape_statement(s) for s in resp.results]
    return {**page_info(resp.total, resp.offset, len(results)), "results": results}


def text_property_names(schema: str) -> tuple[str, ...]:
    """The prose properties of ``schema`` worth reading, e.g. ``notes``, ``reason``."""
    return _prose_names(schema)


def designation_props(schema: str) -> tuple[str, ...]:
    """The :data:`DESIGNATION_SECTIONS` relations ``schema`` actually has."""
    allowed = _property_names(schema, "summary")
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


def page_entries(entries: list[dict[str, Any]], offset: int, budget: int) -> dict[str, Any]:
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
    return {**page_info(total, offset, len(page)), "results": page}


@dataclass
class LegendCodes:
    """The codes a response mentions, gathered for its legend."""

    schemata: set[str] = field(default_factory=set)
    # FtM enum type name (`country`, `language`, …) → the values met.
    values: dict[str, set[str]] = field(default_factory=dict)
    datasets: set[str] = field(default_factory=set)
    programs: set[str] = field(default_factory=set)
    # Property name → {label, type, range}, resolved where the property was met:
    # the same name can mean different things in different schemata (`authority`).
    properties: dict[str, dict[str, str]] = field(default_factory=dict)
    features: set[str] = field(default_factory=set)

    def add_values(self, type_name: str, values: Iterable[str | None]) -> None:
        """Record values of an enum type found outside an entity's properties."""
        self.values.setdefault(type_name, set()).update(v for v in values if v)

    def add_properties(self, schema: str | None, names: Iterable[str]) -> None:
        """Record property names met outside a ``properties`` dict, e.g. relation keys.

        Without a ``schema``, names resolve through the cross-schema map.
        """
        defs = _defs_for(schema)
        for name in names:
            if name in defs:
                self._add_property(name, defs[name])

    def _add_property(self, name: str, prop: _PropDef) -> None:
        label, prop_type, range_ = prop
        if label is None or name in self.properties:
            return
        rec = {"label": label, "type": prop_type, "range": range_}
        self.properties[name] = {k: v for k, v in rec.items() if v is not None}


def collect_codes(obj: Any) -> LegendCodes:
    """Walk a shaped response and gather every code its legend should resolve.

    Reads entity dicts (``schema`` + ``properties``, plus the relation keys and
    match ``explanation`` they carry), and statement rows, description entries
    and source credits (``prop`` / ``value`` / ``dataset`` / ``lang``) anywhere
    in the tree.
    """
    codes = LegendCodes()
    _walk(obj, codes)
    return codes


def build_legend(
    codes: LegendCodes,
    *,
    datasets: Mapping[str, Mapping[str, Any]],
    programs: Mapping[str, Mapping[str, Any]],
    features: Mapping[str, str] | None = None,
    links: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve gathered codes into the response's ``legend``.

    Schema, property and enum-value labels come from the bundled model;
    dataset, program and feature records from the caller's live lookups (see
    :func:`dataset_records`, :func:`program_records`,
    :func:`feature_descriptions`). Enum values land in one section per FtM
    type, keyed by the type's plural (``countries``, ``languages``). Codes
    without a known record are skipped and empty sections omitted, so a failed
    lookup degrades to a thinner legend, never an error.
    """
    program_entries = {k: dict(programs[k]) for k in sorted(codes.programs) if k in programs}
    values = {name: set(found) for name, found in codes.values.items()}
    territories = (p.get("territory") for p in program_entries.values())
    values.setdefault("country", set()).update(t for t in territories if t)
    sections: dict[str, Any] = {
        "links": dict(links or {}),
        "schemata": {s: _schema_record(s) for s in sorted(codes.schemata) if schemas.has_schema(s)},
        "properties": dict(sorted(codes.properties.items())),
    }
    for type_name in sorted(values):
        if type_name in _enum_types():
            section = _resolve(values[type_name], _type_vocabulary(type_name))
            sections[_enum_types()[type_name]] = section
    sections["datasets"] = {n: dict(datasets[n]) for n in sorted(codes.datasets) if n in datasets}
    sections["programs"] = program_entries
    sections["features"] = _resolve(codes.features, features or {})
    return {name: section for name, section in sections.items() if section}


def dataset_records(resp: DatasetsResponse) -> dict[str, dict[str, Any]]:
    """Project the catalog to the per-dataset records the legend cites.

    ``official`` is ``False`` for aggregators, NGOs and Wikidata (the
    website's "Unofficial" badge), and absent for collections, which have no
    publisher. URLs and summaries stay out to keep the legend small: website
    pages follow the ``links`` templates, and ``describe_dataset`` serves the
    rest.
    """
    out: dict[str, dict[str, Any]] = {}
    for d in resp.datasets:
        rec = {
            "title": d.title,
            "publisher": d.publisher.name if d.publisher else None,
            "official": d.publisher.official if d.publisher else None,
        }
        out[d.name] = {k: v for k, v in rec.items() if v is not None}
    return out


def program_records(programs: list[Program]) -> dict[str, dict[str, Any]]:
    """Project the program catalog to the per-program records the legend cites.

    Like :func:`dataset_records`, without URLs: ``describe_program`` serves them.
    """
    out: dict[str, dict[str, Any]] = {}
    for p in programs:
        rec = {
            "title": p.title,
            "issuer": p.issuer.name if p.issuer else None,
            "territory": p.issuer.territory if p.issuer else None,
        }
        out[p.key] = {k: v for k, v in rec.items() if v is not None}
    return out


def freshness(resp: DatasetsResponse) -> dict[str, Any] | None:
    """Return the version and update time of the default collection, if indexed.

    A screen reports this snapshot as its ``as_of`` regardless of the dataset
    it was scoped to: a scoped search still runs against one index build.
    """
    for d in resp.datasets:
        if d.name == DEFAULT_COLLECTION:
            rec = {"version": d.index_version or d.version, "updated_at": _timestamp(d.updated_at)}
            return {k: v for k, v in rec.items() if v is not None} or None
    return None


def feature_descriptions(resp: AlgorithmsResponse) -> dict[str, str]:
    """Map the server-default algorithm's feature names to their descriptions.

    ``default`` may name an alias (``"best"``) rather than an algorithm, so an
    unknown name falls back to the algorithm ``best`` points at.
    """
    by_name = {a.name: a for a in resp.algorithms}
    algorithm = by_name.get(resp.default) or by_name.get(resp.best)
    if algorithm is None:
        return {}
    return {name: doc.description for name, doc in algorithm.features.items() if doc.description}


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
    allowed = _property_names(entity.schema_, "summary")
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
        values = [v for v in entity.properties.get(name, []) if isinstance(v, str)]
        if name == "name" and values == [entity.caption]:
            continue
        if values:
            out[name] = values
    return out


def _prose_values(entity: Entity) -> dict[str, list[str]]:
    """The entity's prose property values, by property name."""
    out: dict[str, list[str]] = {}
    for name in _prose_names(entity.schema_):
        values = [v for v in entity.properties.get(name, []) if isinstance(v, str)]
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
            {
                name
                for ancestor in model["schemata"][schema]["schemata"]
                for name, prop in model["schemata"].get(ancestor, {}).get("properties", {}).items()
                if prop.get("type") in _PROSE_TYPES and not prop.get("hidden")
            }
        )
    )


@functools.cache
def _featured_names(schema: str) -> tuple[str, ...]:
    """``schema``'s FtM featured properties, minus entity- and topic-typed ones."""
    if not schemas.has_schema(schema):
        return ()
    allowed = _property_names(schema, "summary")
    defs = _schema_property_defs(schema)
    return tuple(
        name
        for name in model["schemata"][schema].get("featured") or ()
        if name in allowed and defs[name][1] != "entity"
    )


@functools.cache
def _property_names(schema: str, level: Level) -> frozenset[str]:
    """Names of ``schema``'s properties shown at ``level`` (empty for unknown schemata).

    Both levels share one set; ``stub`` narrows it further to featured names.
    """
    if not schemas.has_schema(schema):
        return frozenset()
    excluded = _BULK_TYPES | {"topic"}
    return frozenset(
        name
        for ancestor in model["schemata"][schema]["schemata"]
        for name, prop in model["schemata"].get(ancestor, {}).get("properties", {}).items()
        if prop.get("type") not in excluded
    )


def _topic_labels(entity: Entity) -> dict[str, str]:
    """Resolve the entity's topic codes to a ``code → label`` map.

    A code missing from the bundled vocabulary (model drift) falls back to
    itself rather than being dropped — a raw code is still signal.
    """
    vocab = _type_vocabulary("topic")
    return {
        code: vocab.get(code, code)
        for code in entity.properties.get("topics", [])
        if isinstance(code, str)
    }


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


def _walk(obj: Any, codes: LegendCodes) -> None:
    if isinstance(obj, list):
        for item in obj:
            _walk(item, codes)
        return
    if not isinstance(obj, dict):
        return
    schema = obj.get("schema")
    props = obj.get("properties")
    if isinstance(schema, str):
        codes.schemata.add(schema)
    if isinstance(schema, str) and isinstance(props, dict):
        codes.datasets.update(d for d in obj.get("datasets", ()) if isinstance(d, str))
        defs = _defs_for(schema)
        for name, values in props.items():
            strings = [v for v in values if isinstance(v, str)]
            _collect_values(name, defs.get(name) or _property_defs().get(name), strings, codes)
        # A profile's relation keys and prose counts name properties of this entity.
        relation_keys = [p for p in PROFILE_SECTIONS if p in obj]
        for key in ("relations", "descriptions"):
            if isinstance(obj.get(key), dict):
                relation_keys.extend(obj[key])
        codes.add_properties(schema, relation_keys)
        if isinstance(obj.get("explanation"), dict):
            codes.features.update(obj["explanation"])
    else:
        # Statement rows, description entries and their source credits carry no
        # schema, so their property names resolve through the global map.
        if isinstance(obj.get("dataset"), str):
            codes.datasets.add(obj["dataset"])
        if isinstance(obj.get("lang"), str):
            codes.add_values("language", [obj["lang"]])
        if isinstance(obj.get("prop"), str):
            values = [obj["value"]] if isinstance(obj.get("value"), str) else []
            _collect_values(obj["prop"], _property_defs().get(obj["prop"]), values, codes)
    for value in obj.values():
        _walk(value, codes)


def _collect_values(
    prop: str, prop_def: _PropDef | None, values: list[str], codes: LegendCodes
) -> None:
    if prop_def is None:
        return
    codes._add_property(prop, prop_def)
    prop_type = prop_def[1]
    if prop_type in _enum_types():
        codes.add_values(prop_type, values)
    elif prop == "programId":
        codes.programs.update(values)


def _defs_for(schema: str | None) -> dict[str, _PropDef]:
    """Property definitions for ``schema``, or the cross-schema map without one."""
    if schema is not None and schemas.has_schema(schema):
        return _schema_property_defs(schema)
    return _property_defs()


def _prop_def(prop: Mapping[str, Any]) -> _PropDef:
    return (prop.get("label"), prop.get("type"), prop.get("range"))


@functools.cache
def _schema_property_defs(schema: str) -> dict[str, _PropDef]:
    """Property name → definition for ``schema``, own and inherited."""
    defs: dict[str, _PropDef] = {}
    for ancestor in model["schemata"][schema]["schemata"]:
        for name, prop in model["schemata"].get(ancestor, {}).get("properties", {}).items():
            defs.setdefault(name, _prop_def(prop))
    return defs


@functools.cache
def _property_defs() -> dict[str, _PropDef]:
    """Property name → definition across every schema; first definition wins.

    Only for names with no schema context (statement rows, description
    entries, a single relation page). A few names differ between schemata —
    `authority` is a string on Sanction but an entity on Contract — so anything
    with a schema uses :func:`_schema_property_defs` instead.
    """
    defs: dict[str, _PropDef] = {}
    for schema in sorted(model["schemata"]):
        for name, prop in model["schemata"][schema].get("properties", {}).items():
            defs.setdefault(name, _prop_def(prop))
    return defs


def _schema_record(schema: str) -> dict[str, Any]:
    spec = model["schemata"][schema]
    rec = {
        "label": spec.get("label"),
        "plural": spec.get("plural"),
        "ancestors": sorted(set(spec.get("schemata", ())) - {schema}),
    }
    return {k: v for k, v in rec.items() if v}


@functools.cache
def _enum_types() -> dict[str, str]:
    """FtM types with a value vocabulary (except topic) → their legend section name.

    Topics are left out: their labels sit inline on each entity.
    """
    return {
        name: str(spec.get("plural") or name).lower()
        for name, spec in model["types"].items()
        if spec.get("values") and name != "topic"
    }


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@functools.cache
def _type_vocabulary(type_name: str) -> dict[str, str]:
    """Cached ``value → label`` map for an FtM enum type."""
    return schemas.type_values(type_name)


def _resolve(codes: set[str], labels: Mapping[str, str]) -> dict[str, str]:
    return {code: labels[code] for code in sorted(codes) if code in labels}
