"""Build the ``legend`` that resolves every code in an MCP response.

Topic labels sit inline on each entity (see :mod:`~yente_client.mcp.shaping`);
every other code — schemata, property names, enum values such as countries
and languages, dataset and program keys, match features — resolves through
one ``legend`` per response. It is built by walking the finished output
(:func:`collect_codes`, :func:`build_legend`), so it describes only codes the
response contains. Schema, property and value labels come from the bundled
model; dataset, program and feature records from live lookups the server
passes in. The legend also says how to link to an entity (``links``).

Pure functions — no FastMCP, unit-testable.
"""

import functools
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from yente_client import schemas
from yente_client._util import compact
from yente_client.mcp.shaping import PROFILE_SECTIONS
from yente_client.models import Algorithm, AlgorithmsResponse, Dataset, DatasetsResponse, Program
from yente_client.schemas import (
    PropertySpec,
    all_property_specs,
    model,
    property_specs,
    schema_spec,
    type_spec,
)


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
        specs = _specs_for(schema)
        for name in names:
            if name in specs:
                self.add_property(name, specs[name])

    def add_property(self, name: str, prop: PropertySpec) -> None:
        """Record one property's label, type and range; the first definition met wins."""
        if prop.label is None or name in self.properties:
            return
        rec = {"label": prop.label, "type": prop.type, "range": prop.range}
        self.properties[name] = compact(rec)

    def add_property_values(
        self, name: str, prop: PropertySpec | None, values: Iterable[str]
    ) -> None:
        """Record a property met in the output, plus whichever of its values are codes."""
        if prop is None:
            return
        self.add_property(name, prop)
        if prop.type in _enum_types():
            self.add_values(prop.type, values)
        elif name == "programId":
            self.programs.update(values)


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
    as_of: str | None = None,
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
        "as_of": as_of,
        "schemata": {s: _schema_record(s) for s in sorted(codes.schemata) if schemas.has_schema(s)},
        "properties": dict(sorted(codes.properties.items())),
    }
    for type_name in sorted(values):
        if type_name in _enum_types():
            section = _resolve(values[type_name], type_spec(type_name).values)
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
    pages follow the ``links`` templates, and ``describe`` serves the
    rest.
    """
    out: dict[str, dict[str, Any]] = {}
    for d in resp.datasets:
        rec = {
            "title": d.title,
            "publisher": d.publisher.name if d.publisher else None,
            "official": d.publisher.official if d.publisher else None,
        }
        out[d.name] = compact(rec)
    return out


def program_records(programs: list[Program]) -> dict[str, dict[str, Any]]:
    """Project the program catalog to the per-program records the legend cites.

    Like :func:`dataset_records`, without URLs: ``describe`` serves them.
    """
    out: dict[str, dict[str, Any]] = {}
    for p in programs:
        rec = {
            "title": p.title,
            "issuer": p.issuer.name if p.issuer else None,
            "territory": p.issuer.territory if p.issuer else None,
        }
        out[p.key] = compact(rec)
    return out


def dataset_codes(dataset: Dataset) -> LegendCodes:
    """Gather the codes of one ``describe(kind="dataset")`` record."""
    codes = LegendCodes()
    if dataset.coverage is not None:
        codes.add_values("country", dataset.coverage.countries)
    if dataset.publisher is not None:
        codes.add_values("country", [dataset.publisher.country])
    return codes


def program_codes(program: Program) -> LegendCodes:
    """Gather the codes of one ``describe(kind="program")`` record."""
    codes = LegendCodes()
    territory = program.issuer.territory if program.issuer else None
    codes.add_values("country", [territory, *program.target_territories])
    if program.dataset:
        codes.datasets.add(program.dataset)
    return codes


def as_of(resp: DatasetsResponse, dataset: str) -> str | None:
    """Return the update time of the server's collection, for the legend's ``as_of``.

    Responses report this one time whatever dataset a call was scoped to: a
    scoped query still runs against one index build.
    """
    for d in resp.datasets:
        if d.name == dataset:
            return d.updated_at.isoformat() if d.updated_at is not None else None
    return None


def feature_descriptions(resp: AlgorithmsResponse) -> dict[str, str]:
    """Map the server-default algorithm's feature names to their descriptions."""
    algorithm = _default_algorithm(resp)
    if algorithm is None:
        return {}
    return {name: doc.description for name, doc in algorithm.features.items() if doc.description}


def feature_coefficients(resp: AlgorithmsResponse) -> dict[str, float]:
    """Map the server-default algorithm's feature names to their signed weights.

    Penalties carry negative coefficients; a feature's contribution to a
    candidate's score is its unsigned score times this weight.
    """
    algorithm = _default_algorithm(resp)
    if algorithm is None:
        return {}
    return {
        name: doc.coefficient
        for name, doc in algorithm.features.items()
        if doc.coefficient is not None
    }


def _default_algorithm(resp: AlgorithmsResponse) -> Algorithm | None:
    """Pick the server-default algorithm.

    ``default`` may name an alias (``"best"``) rather than an algorithm, so an
    unknown name falls back to the algorithm ``best`` points at.
    """
    by_name = {a.name: a for a in resp.algorithms}
    return by_name.get(resp.default) or by_name.get(resp.best)


def _walk(obj: Any, codes: LegendCodes) -> None:
    if isinstance(obj, list):
        for item in obj:
            _walk(item, codes)
        return
    if not isinstance(obj, dict):
        return
    schema = obj.get("schema")
    if isinstance(schema, str):
        codes.schemata.add(schema)
        codes.datasets.update(d for d in obj.get("datasets", ()) if isinstance(d, str))
        specs = _specs_for(schema)
        props = obj.get("properties")
        for name, values in props.items() if isinstance(props, dict) else ():
            strings = [v for v in values if isinstance(v, str)]
            spec = specs.get(name) or all_property_specs().get(name)
            codes.add_property_values(name, spec, strings)
        # A profile's section and relation keys name properties of this entity.
        relation_keys = [p for p in PROFILE_SECTIONS if p in obj]
        if isinstance(obj.get("relations"), dict):
            relation_keys.extend(obj["relations"])
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
            codes.add_property_values(obj["prop"], all_property_specs().get(obj["prop"]), values)
    for value in obj.values():
        _walk(value, codes)


def _specs_for(schema: str | None) -> Mapping[str, PropertySpec]:
    """Property specs for ``schema``, or the cross-schema map without one."""
    if schema is not None and schemas.has_schema(schema):
        return property_specs(schema)
    return all_property_specs()


def _schema_record(schema: str) -> dict[str, Any]:
    spec = schema_spec(schema)
    rec = {
        "label": spec.label,
        "plural": spec.plural,
        "ancestors": sorted(set(spec.schemata) - {schema}),
    }
    return compact(rec)


@functools.cache
def _enum_types() -> dict[str, str]:
    """FtM types with a value vocabulary (except topic) → their legend section name.

    Topics are left out: their labels sit inline on each entity.
    """
    specs = {name: type_spec(name) for name in model["types"]}
    return {
        name: (spec.plural or name).lower()
        for name, spec in specs.items()
        if spec.values and name != "topic"
    }


def _resolve(codes: set[str], labels: Mapping[str, str]) -> dict[str, str]:
    return {code: labels[code] for code in sorted(codes) if code in labels}
