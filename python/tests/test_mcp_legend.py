"""Tests for the legend that resolves every code in an MCP response.

Pure functions over shaped output and the SDK response models — no network,
no FastMCP.
"""

from yente_client.mcp.legend import (
    LegendCodes,
    as_of,
    build_legend,
    collect_codes,
    dataset_records,
    feature_descriptions,
    program_records,
)
from yente_client.mcp.shaping import shape, shape_profile, shape_scored
from yente_client.models import (
    AdjacentPropertyResponse,
    AdjacentResponse,
    AlgorithmsResponse,
    DatasetsResponse,
    Entity,
    FeatureResult,
    Program,
    ProgramIssuer,
    ScoredEntity,
    TotalSpec,
)
from yente_client.schemas import type_values


def _total(value: int) -> TotalSpec:
    return TotalSpec(value=value, relation="eq")


def _page(results: list[str | Entity], total: int, offset: int = 0) -> AdjacentPropertyResponse:
    return AdjacentPropertyResponse(results=results, total=_total(total), limit=50, offset=offset)


def _sanction(id: str, source: str = "P1", **props: list[str]) -> Entity:
    return Entity(
        id=id, caption="Sanction", schema="Sanction", properties={"entity": [source], **props}
    )


def test_legend_gathers_codes_across_the_response() -> None:
    asset = Entity(id="c1", caption="ACME", schema="Company", properties={"jurisdiction": ["cy"]})
    edge = _sanction("s1", programId=["US-RUSHAR"], country=["us"])
    edge.datasets = ["us_ofac_sdn"]
    entity = Entity(
        id="P1",
        caption="Jane",
        schema="Person",
        properties={"nationality": ["ru"], "registrationNumber": ["ru"]},
        datasets=["eu_fsf"],
    )
    adjacency = AdjacentResponse(
        entity=entity,
        adjacent={
            "sanctions": _page([edge], total=1),
            "ownershipOwner": _page(
                [
                    Entity(
                        id="o1",
                        caption="Ownership",
                        schema="Ownership",
                        properties={"asset": [asset]},
                    )
                ],
                total=1,
            ),
        },
    )
    codes = collect_codes(shape_profile(entity, adjacency))
    # a country-looking registration number stays out; a ref shows no values
    assert codes.values["country"] == {"ru", "us"}
    assert codes.datasets == {"eu_fsf", "us_ofac_sdn"}
    assert codes.programs == {"US-RUSHAR"}
    assert {"nationality", "programId", "asset"} <= codes.properties.keys()
    assert {"Person", "Sanction", "Ownership", "Company"} <= codes.schemata
    # relation keys of the profile resolve against the entity's own schema
    assert codes.properties["sanctions"]["range"] == "Sanction"
    assert codes.properties["ownershipOwner"]["type"] == "entity"
    # a property's range alone doesn't put its schema in the legend
    assert "Asset" not in codes.schemata


def test_legend_labels_properties_by_their_own_schema() -> None:
    # `authority` is a string on Sanction but an entity on Contract.
    edge = shape(_sanction("s1", authority=["Council"]), source_id="P1")
    assert edge["properties"] == {"authority": ["Council"]}
    labels = build_legend(collect_codes(edge), datasets={}, programs={})["properties"]
    assert labels["authority"] == {"label": "Authority", "type": "string"}


def test_legend_reads_statement_rows() -> None:
    rows = {
        "results": [
            {"prop": "nationality", "value": "ru", "dataset": "eu_fsf"},
            {"prop": "programId", "value": "EU-UKR", "dataset": "eu_fsf"},
            {"prop": "name", "value": "Иван", "dataset": "eu_fsf", "lang": "rus"},
        ]
    }
    codes = collect_codes(rows)
    assert codes.values["country"] == {"ru"}
    assert codes.values["language"] == {"rus"}
    assert codes.programs == {"EU-UKR"}
    assert codes.datasets == {"eu_fsf"}


def test_build_legend_resolves_and_omits_empty_sections() -> None:
    codes = collect_codes(
        {
            "schema": "Person",
            "properties": {"birthDate": ["1970"], "nationality": ["ru"]},
            "datasets": ["x"],
        }
    )
    legend = build_legend(codes, datasets={"x": {"title": "Dataset X"}}, programs={})
    assert legend["countries"] == {"ru": type_values("country")["ru"]}
    assert legend["datasets"] == {"x": {"title": "Dataset X"}}
    assert legend["properties"]["birthDate"]["type"] == "date"
    assert "programs" not in legend
    assert "as_of" not in legend and "links" not in legend
    # failure-soft: missing records degrade to a thinner legend
    assert "datasets" not in build_legend(codes, datasets={}, programs={})


def test_legend_describes_schemata_with_ancestors() -> None:
    codes = collect_codes({"schema": "Person", "properties": {}})
    person = build_legend(codes, datasets={}, programs={})["schemata"]["Person"]
    assert person["label"] == "Person"
    assert {"LegalEntity", "Thing"} <= set(person["ancestors"])
    assert "Person" not in person["ancestors"]


def test_legend_names_value_sections_by_type_plural() -> None:
    codes = collect_codes(
        {"schema": "Person", "properties": {"gender": ["male"], "nationality": ["ru"]}}
    )
    legend = build_legend(codes, datasets={}, programs={})
    assert legend["genders"] == {"male": type_values("gender")["male"]}
    assert "ru" in legend["countries"]
    # topics resolve inline on the entity, never in the legend
    assert "topics" not in legend


def test_legend_resolves_features_present_in_explanations() -> None:
    candidate = ScoredEntity(
        id="Q1",
        caption="Jane",
        schema="Person",
        properties={},
        score=0.9,
        match=True,
        explanations={"name_match": FeatureResult(score=0.9)},
    )
    codes = collect_codes({"results": [shape_scored(candidate)]})
    assert codes.features == {"name_match"}
    features = {"name_match": "Compares names", "dob_year_disjoint": "Birth years differ"}
    legend = build_legend(codes, datasets={}, programs={}, features=features)
    assert legend["features"] == {"name_match": "Compares names"}


def test_legend_carries_links() -> None:
    legend = build_legend(
        LegendCodes(), datasets={}, programs={}, links={"entity": "https://example.org/{id}"}
    )
    assert legend == {"links": {"entity": "https://example.org/{id}"}}


def test_legend_programs_add_their_territory() -> None:
    codes = collect_codes({"prop": "programId", "value": "US-RUSHAR"})
    programs = program_records(
        [
            Program(
                key="US-RUSHAR", title="Russia", issuer=ProgramIssuer(name="OFAC", territory="us")
            )
        ]
    )
    legend = build_legend(codes, datasets={}, programs=programs)
    assert legend["programs"]["US-RUSHAR"] == {
        "title": "Russia",
        "issuer": "OFAC",
        "territory": "us",
    }
    assert "us" in legend["countries"]


def test_dataset_records_mark_officialness_without_urls(load_fixture) -> None:
    catalog = DatasetsResponse.model_validate(load_fixture("catalog"))
    records = dataset_records(catalog)
    assert records["us_ofac_sdn"] == {
        "title": "US OFAC Specially Designated Nationals",
        "publisher": "Office of Foreign Assets Control",
        "official": True,
    }
    assert "official" not in records["sanctions"]  # a collection has no publisher


def test_as_of_reports_the_named_collection(load_fixture) -> None:
    catalog = DatasetsResponse.model_validate(load_fixture("catalog"))
    updated = as_of(catalog, "default")
    assert updated is not None and updated.startswith("2026-05-30")
    assert as_of(catalog, "no_such_collection") is None
    assert as_of(DatasetsResponse(datasets=[]), "default") is None


def test_build_legend_carries_as_of() -> None:
    legend = build_legend(LegendCodes(), datasets={}, programs={}, as_of="2026-05-30T00:00:00")
    assert legend == {"as_of": "2026-05-30T00:00:00"}


def test_feature_descriptions_follow_the_default_alias(load_fixture) -> None:
    resp = AlgorithmsResponse.model_validate(load_fixture("algorithms"))
    features = feature_descriptions(resp)
    assert features["name_match"].startswith("Match two entities")
    assert "dob_year_disjoint" in features
