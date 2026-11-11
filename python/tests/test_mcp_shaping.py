"""Tests for the entity projection the MCP tools return.

Pure functions over the SDK response models — no network, no FastMCP.
"""

from datetime import datetime

from yente_client.mcp.shaping import (
    LegendCodes,
    build_legend,
    collect_codes,
    dataset_records,
    feature_descriptions,
    freshness,
    page_entries,
    page_info,
    program_records,
    shape,
    shape_adjacency,
    shape_profile,
    shape_raw,
    shape_relation_page,
    shape_scored,
    shape_statements,
    text_entries,
    unknown_name,
)
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
    Statement,
    StatementsResponse,
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


def test_shape_summary_keeps_identity_dates_and_datasets() -> None:
    entity = Entity(
        id="Q1",
        caption="Jane Doe",
        schema="Person",
        properties={"name": ["Jane Doe"]},
        datasets=["us_ofac_sdn"],
        first_seen=datetime(2022, 3, 1, 10, 0),
        last_change=datetime(2026, 10, 13, 8, 30),
    )
    shaped = shape(entity)
    assert shaped == {
        "id": "Q1",
        "caption": "Jane Doe",
        "schema": "Person",
        "datasets": ["us_ofac_sdn"],
        "properties": {"name": ["Jane Doe"]},
        "first_seen": "2022-03-01",
        "last_change": "2026-10-13",
    }


def test_shape_summary_picks_properties_by_schema() -> None:
    vessel = Entity(
        id="V1",
        caption="ANGARA",
        schema="Vessel",
        properties={"name": ["ANGARA"], "imoNumber": ["IMO9179842"], "flag": ["ru"]},
    )
    assert shape(vessel)["properties"] == {
        "name": ["ANGARA"],
        "imoNumber": ["IMO9179842"],
        "flag": ["ru"],
    }
    person = Entity(
        id="P1",
        caption="x",
        schema="Person",
        properties={"birthPlace": ["Leningrad"], "citizenship": ["ru"], "gender": ["male"]},
    )
    assert set(shape(person)["properties"]) == {"birthPlace", "citizenship", "gender"}


def test_shape_summary_drops_prose() -> None:
    entity = Entity(
        id="P1",
        caption="x",
        schema="Person",
        properties={"name": ["x"], "notes": ["long biography"]},
    )
    assert shape(entity)["properties"] == {"name": ["x"]}


def test_shape_summary_drops_prose_on_relation_records() -> None:
    edge = _sanction("s1", reason=["sanctioned because X"], authority=["OFAC"])
    props = shape(edge, source_id="P1")["properties"]
    assert props == {"authority": ["OFAC"]}  # reason left out, back-ref dropped


def test_shape_ignores_unknown_properties_and_schemata() -> None:
    entity = Entity(id="Q1", caption="x", schema="Person", properties={"bogus": ["v"]})
    assert shape(entity)["properties"] == {}
    unknown = Entity(id="Q2", caption="x", schema="NoSuchSchema", properties={"name": ["x"]})
    assert shape(unknown)["properties"] == {}


def test_shape_resolves_topics_inline_at_every_level() -> None:
    entity = Entity(
        id="Q1",
        caption="x",
        schema="Person",
        properties={"topics": ["sanction", "not.a.real.topic"]},
    )
    labels = type_values("topic")
    for level in ("stub", "summary"):
        shaped = shape(entity, level)
        assert shaped["topics"] == {
            "sanction": labels["sanction"],
            "not.a.real.topic": "not.a.real.topic",
        }
        assert "topics" not in shaped.get("properties", {})  # promoted, not duplicated


def test_shape_stub_is_identity_topics_and_featured_properties() -> None:
    entity = Entity(
        id="c1",
        caption="ACME",
        schema="Company",
        properties={
            "topics": ["sanction"],
            "name": ["ACME"],
            "jurisdiction": ["cy"],
            "notes": ["not featured"],
        },
        datasets=["x"],
    )
    assert shape(entity, "stub") == {
        "id": "c1",
        "caption": "ACME",
        "schema": "Company",
        "topics": {"sanction": type_values("topic")["sanction"]},
        "properties": {"jurisdiction": ["cy"]},  # name only repeats the caption
        "abridged": True,
    }


def test_shape_stub_gives_a_position_its_country() -> None:
    post = Entity(
        id="pos1",
        caption="Member of the Duma",
        schema="Position",
        properties={"name": ["Member of the Duma"], "country": ["ru"]},
    )
    occupancy = Entity(
        id="occ1",
        caption="Occupancy",
        schema="Occupancy",
        properties={"holder": ["P1"], "post": [post], "status": ["current"]},
    )
    rec = shape(occupancy, source_id="P1")
    assert rec["properties"]["post"][0]["properties"]["country"] == ["ru"]
    assert collect_codes(rec).values["country"] == {"ru"}


def test_shape_stub_omits_empty_properties_and_entity_refs() -> None:
    ownership = Entity(
        id="o1", caption="Ownership", schema="Ownership", properties={"owner": ["P1"]}
    )
    assert "properties" not in shape(ownership, "stub")


def test_shape_scored_carries_explanation_evidence() -> None:
    scored = ScoredEntity(
        id="Q1",
        caption="Abdul El-Sayed",
        schema="Person",
        properties={"name": ["Abdul El-Sayed"]},
        score=0.75,
        match=True,
        explanations={
            "name_literal_match": FeatureResult(score=0.9, detail="exact name match"),
            "dob_day_disjoint": FeatureResult(
                score=-0.15, detail="birth day mismatch", query="1984-01-05", candidate="1984-10-31"
            ),
        },
    )
    shaped = shape_scored(scored)
    assert (shaped["score"], shaped["match"]) == (0.75, True)
    assert shaped["explanation"] == {
        "name_literal_match": {"score": 0.9, "detail": "exact name match"},
        "dob_day_disjoint": {
            "score": -0.15,
            "detail": "birth day mismatch",
            "query": "1984-01-05",
            "candidate": "1984-10-31",
        },
    }


def test_shape_scored_caps_positives_but_keeps_every_negative() -> None:
    scored = ScoredEntity(
        id="Q1",
        caption="x",
        schema="Person",
        score=0.5,
        match=False,
        explanations={
            "a": FeatureResult(score=0.1),
            "b": FeatureResult(score=0.9),
            "c": FeatureResult(score=0.5),
            "d": FeatureResult(score=0.7),
            "pen1": FeatureResult(score=-0.05),
            "pen2": FeatureResult(score=-0.95),
            "zero": FeatureResult(score=0.0),
        },
    )
    explanation = shape_scored(scored)["explanation"]
    # top 3 positives plus both penalties, ranked by |score|; zero and the weakest positive dropped
    assert list(explanation) == ["pen2", "b", "d", "c", "pen1"]


def test_shape_relation_edge_refs_counterparty_with_topics() -> None:
    asset = Entity(
        id="c1",
        caption="ACME Ltd",
        schema="Company",
        properties={"topics": ["sanction"], "jurisdiction": ["cy"]},
    )
    edge = Entity(
        id="own1",
        caption="Ownership",
        schema="Ownership",
        datasets=["ext_ru_egrul"],
        properties={"owner": ["P1"], "asset": [asset], "percentage": ["100"]},
        first_seen=datetime(2026, 10, 13),
    )
    out = shape_relation_page(_page([edge, "unresolved-id"], total=2), "P1", "ownershipOwner")
    rec = out["results"][0]
    assert rec["datasets"] == ["ext_ru_egrul"]
    assert rec["caption"] == "Ownership"
    assert "first_seen" not in rec  # dates only on the top-level record
    assert "owner" not in rec["properties"]
    assert rec["properties"]["percentage"] == ["100"]
    assert rec["properties"]["asset"] == [
        {
            "id": "c1",
            "caption": "ACME Ltd",
            "schema": "Company",
            "topics": {"sanction": type_values("topic")["sanction"]},
            "abridged": True,
        }
    ]
    assert "abridged" not in rec  # the edge itself is shown in full
    assert out["results"][1] == {"id": "unresolved-id"}


def test_shape_relation_page_keeps_position_post_stubs() -> None:
    post = Entity(
        id="pos1",
        caption="Member of the State Duma",
        schema="Position",
        properties={"name": ["Member of the State Duma"], "country": ["ru"]},
    )
    occupancy = Entity(
        id="occ1",
        caption="Occupancy",
        schema="Occupancy",
        properties={"holder": ["P1"], "post": [post], "startDate": ["2016"]},
        first_seen=datetime(2024, 1, 1),
    )
    out = shape_relation_page(_page([occupancy], total=1), "P1", "positionOccupancies")
    row = out["results"][0]
    assert row["properties"]["post"][0]["properties"] == {"country": ["ru"]}
    assert row["properties"]["startDate"] == ["2016"]
    assert "first_seen" not in row


def test_shape_relation_page_drops_designation_captions() -> None:
    sanction = _sanction("s1", program=["Reciprocal"], authority=["OFAC"])
    sanction.caption = "Reciprocal"
    sanction.first_seen = datetime(2024, 1, 1)
    row = shape_relation_page(_page([sanction], total=1), "P1", "sanctions")["results"][0]
    assert "caption" not in row
    assert "first_seen" not in row
    assert row["properties"] == {"program": ["Reciprocal"], "authority": ["OFAC"]}


def test_only_refs_and_stubs_are_marked_abridged() -> None:
    entity = Entity(id="P1", caption="Jane", schema="Person", properties={"name": ["Jane"]})
    assert shape(entity, "ref")["abridged"] is True
    assert shape(entity, "stub")["abridged"] is True
    assert "abridged" not in shape(entity)


def test_top_level_records_keep_their_dates() -> None:
    entity = Entity(id="P1", caption="Jane", schema="Person", first_seen=datetime(2022, 3, 1))
    profile = shape_profile(entity, AdjacentResponse(entity=entity, adjacent={}))
    assert profile["first_seen"] == "2022-03-01"
    scored = ScoredEntity(
        id="Q1",
        caption="x",
        schema="Person",
        score=0.9,
        match=True,
        first_seen=datetime(2023, 1, 1),
    )
    assert shape_scored(scored)["first_seen"] == "2023-01-01"


def test_page_info_marks_truncation() -> None:
    assert page_info(_total(29), 0, 5) == {
        "total": 29,
        "returned": 5,
        "offset": 0,
        "next_offset": 5,
    }
    assert page_info(_total(29), 25, 4)["next_offset"] is None
    assert page_info(_total(0), 0, 0)["next_offset"] is None


def test_shape_adjacency_keys_by_edge_type_and_drops_riskSource() -> None:
    resp = AdjacentResponse(
        entity=Entity(id="P1", caption="Jane", schema="Person"),
        adjacent={
            "ownershipOwner": _page([], total=2),
            # Reports a count but resolves no entities — must be suppressed.
            "riskSource": _page([], total=33),
        },
    )
    out = shape_adjacency(resp)
    assert set(out) == {"ownershipOwner"}
    assert out["ownershipOwner"]["total"] == 2


def test_shape_profile_inlines_sanctions_and_summarises_relations() -> None:
    entity = Entity(
        id="P1",
        caption="Jane",
        schema="Person",
        properties={"name": ["Jane"], "notes": ["bio"]},
        referents=["ofac-1"],
    )
    adjacency = AdjacentResponse(
        entity=entity,
        adjacent={
            "sanctions": _page([_sanction("s1")], total=1),
            "familyPerson": _page([], total=4),
        },
    )
    profile = shape_profile(entity, adjacency)
    assert "notes" not in profile["properties"]
    assert "referents" not in profile
    assert profile["sanctions"]["results"][0]["id"] == "s1"
    assert set(profile["relations"]) == {"familyPerson"}


def test_shape_profile_prefers_full_pages_for_capped_sections() -> None:
    entity = Entity(id="P1", caption="Jane", schema="Person")
    adjacency = AdjacentResponse(
        entity=entity, adjacent={"sanctions": _page([_sanction("s1")], total=2)}
    )
    full = _page([_sanction("s1"), _sanction("s2")], total=2)
    profile = shape_profile(entity, adjacency, {"sanctions": full})
    assert [s["id"] for s in profile["sanctions"]["results"]] == ["s1", "s2"]
    assert profile["sanctions"]["next_offset"] is None
    assert "relations" not in profile


def test_shape_profile_lifts_risks_and_positions_beside_sanctions() -> None:
    entity = Entity(id="P1", caption="Jane", schema="Person")
    risk = Entity(
        id="r1",
        caption="Risk",
        schema="Risk",
        properties={
            "entity": ["P1"],
            "topics": ["poi"],
            "reason": ["named in an oligarch list"],
            "status": ["active"],
        },
    )
    occupancy = Entity(
        id="occ1", caption="Occupancy", schema="Occupancy", properties={"holder": ["P1"]}
    )
    adjacency = AdjacentResponse(
        entity=entity,
        adjacent={
            "risks": _page([risk], total=1),
            "positionOccupancies": _page([occupancy], total=1),
            "familyPerson": _page([], total=1),
        },
    )
    profile = shape_profile(entity, adjacency)
    assert "sanctions" not in profile  # only sections the entity has
    rec = profile["risks"]["results"][0]
    assert rec["topics"] == {"poi": type_values("topic")["poi"]}
    assert rec["properties"] == {"status": ["active"]}
    assert profile["descriptions"] == {"reason": 1}
    assert profile["positionOccupancies"]["results"][0]["id"] == "occ1"
    assert set(profile["relations"]) == {"familyPerson"}


def test_shape_raw_keeps_dump_and_adds_topic_labels() -> None:
    entity = Entity(
        id="Q1",
        caption="Jane Doe",
        schema="Person",
        properties={"notes": ["kept"], "topics": ["sanction"]},
        referents=["ofac-1234"],
    )
    record = shape_raw(entity)
    assert record["properties"]["notes"] == ["kept"]
    assert record["referents"] == ["ofac-1234"]
    assert record["topics"] == {"sanction": type_values("topic")["sanction"]}


def test_shape_statements_projects_provenance_rows() -> None:
    resp = StatementsResponse(
        results=[
            Statement(
                id="s1",
                entity_id="ofac-45937",
                canonical_id="NK-abc",
                prop="alias",
                prop_type="name",
                schema="Person",
                value="A. Zakharov",
                original_value="ZAKHAROV, A.",
                dataset="us_ofac_sdn",
                lang="eng",
                origin="patch",
                first_seen=datetime(2023, 7, 19, 18, 2, 43),
                last_seen=datetime(2026, 3, 25, 12, 53, 9),
            ),
            Statement(
                id="s2",
                entity_id="eu-fsf-12789",
                canonical_id="NK-abc",
                prop="birthDate",
                prop_type="date",
                schema="Person",
                value="1965-09-21",
                original_value="1965-09-21",  # unchanged by cleaning → omitted
                dataset="eu_fsf",
                first_seen=datetime(2024, 1, 1),
                last_seen=datetime(2026, 1, 1),
            ),
        ],
        total=_total(2),
        limit=50,
        offset=0,
    )
    out = shape_statements(resp)
    assert (out["total"], out["returned"], out["next_offset"]) == (2, 2, None)
    assert out["results"][0] == {
        "prop": "alias",
        "value": "A. Zakharov",
        "dataset": "us_ofac_sdn",
        "entity_id": "ofac-45937",
        "first_seen": "2023-07-19",
        "last_seen": "2026-03-25",
        "lang": "eng",
        "original_value": "ZAKHAROV, A.",
        "origin": "patch",
    }
    second = out["results"][1]
    assert "canonical_id" not in second  # per-row canonical dropped (it's the query)
    assert "original_value" not in second  # same as value → omitted
    assert "lang" not in second
    assert "origin" not in second  # unset for plain crawled data


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
    # a country-looking registration number stays out; a ref carries no properties
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
    assert "freshness" not in legend and "links" not in legend
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


def test_shape_profile_counts_prose_left_out() -> None:
    entity = Entity(
        id="P1", caption="Jane", schema="Person", properties={"notes": ["a", "b"], "summary": ["c"]}
    )
    adjacency = AdjacentResponse(
        entity=entity,
        adjacent={"sanctions": _page([_sanction("s1", reason=["r1"]), _sanction("s2")], total=2)},
    )
    assert shape_profile(entity, adjacency)["descriptions"] == {
        "notes": 2,
        "summary": 1,
        "reason": 1,
    }
    bare = Entity(id="P2", caption="x", schema="Person")
    assert "descriptions" not in shape_profile(bare, AdjacentResponse(entity=bare, adjacent={}))


def _statement(prop: str, value: str, dataset: str, *, lang: str | None = None) -> Statement:
    return Statement(
        id=f"{dataset}-{prop}-{value}",
        entity_id=f"{dataset}-src",
        canonical_id="P1",
        prop=prop,
        prop_type="text",
        schema="Person",
        value=value,
        dataset=dataset,
        lang=lang,
        first_seen=datetime(2024, 1, 1),
        last_seen=datetime(2026, 1, 1),
    )


def test_text_entries_order_reasons_first_then_official_sources() -> None:
    entity = Entity(id="P1", caption="Jane", schema="Person")
    sanction = _sanction(
        "s1", reason=["Close to the regime."], authority=["Council"], program=["EU-UKR"]
    )
    sanction.datasets = ["eu_fsf"]
    sanction.first_seen = datetime(2014, 3, 17)
    risk = Entity(
        id="r1",
        caption="Risk",
        schema="Risk",
        properties={"entity": ["P1"], "reason": ["On a list."]},
    )
    risk.datasets = ["wd_oligarchs"]
    statements = [
        _statement("notes", "From an NGO.", "ir_uani_business_registry"),
        _statement("notes", "From OFAC.", "us_ofac_sdn"),
    ]
    entries = text_entries(
        entity,
        statements,
        {"sanctions": [sanction], "risks": [risk]},
        unofficial={"ir_uani_business_registry", "wd_oligarchs"},
    )
    assert [e["text"] for e in entries] == [
        "Close to the regime.",
        "On a list.",
        "From OFAC.",
        "From an NGO.",
    ]
    reason = entries[0]
    assert reason["prop"] == "reason"
    assert reason["sources"] == [{"dataset": "eu_fsf", "first_seen": "2014-03-17"}]
    assert reason["records"][0]["id"] == "s1"
    assert reason["records"][0]["properties"]["authority"] == ["Council"]
    assert reason["records"][0]["properties"]["program"] == ["EU-UKR"]


def test_text_entries_put_a_designation_reason_before_its_other_prose() -> None:
    entity = Entity(id="P1", caption="Jane", schema="Person")
    sanction = _sanction("s1", summary=["Instrument of designation."], reason=["The reason."])
    sanction.datasets = ["au_dfat_sanctions"]
    entries = text_entries(entity, [], {"sanctions": [sanction]}, unofficial=set())
    assert [e["prop"] for e in entries] == ["reason", "summary"]


def test_text_entries_group_identical_texts_and_keep_translations_apart() -> None:
    entity = Entity(id="P1", caption="Jane", schema="Person")
    statements = [
        _statement("notes", "Same text.", "gb_fcdo_sanctions", lang="eng"),
        _statement("notes", "Same text.", "ch_seco_sanctions", lang="eng"),
        _statement("notes", "Même texte.", "fr_tresor_gels_avoir", lang="fra"),
    ]
    entries = text_entries(entity, statements, {}, unofficial=set())
    assert len(entries) == 2
    same = next(e for e in entries if e["text"] == "Same text.")
    assert [s["dataset"] for s in same["sources"]] == ["ch_seco_sanctions", "gb_fcdo_sanctions"]
    assert next(e for e in entries if e["text"] == "Même texte.")["lang"] == "fra"


def test_text_entries_fall_back_to_the_record_without_credits() -> None:
    entity = Entity(id="P1", caption="Jane", schema="Person", properties={"notes": ["A note."]})
    entries = text_entries(entity, None, {}, unofficial=set())
    assert entries == [{"prop": "notes", "text": "A note.", "sources": []}]


def test_page_entries_respects_budget_and_marks_the_end() -> None:
    entries = [{"prop": "notes", "text": "x" * 100, "sources": []} for _ in range(5)]
    first = page_entries(entries, 0, budget=300)
    assert (first["returned"], first["next_offset"]) == (2, 2)
    last = page_entries(entries, 4, budget=300)
    assert (last["returned"], last["next_offset"]) == (1, None)
    huge = page_entries([{"prop": "notes", "text": "x" * 1000, "sources": []}], 0, budget=10)
    assert huge["returned"] == 1  # an oversized entry still fills a page


def test_dataset_records_mark_officialness_without_urls(load_fixture) -> None:
    catalog = DatasetsResponse.model_validate(load_fixture("catalog"))
    records = dataset_records(catalog)
    assert records["us_ofac_sdn"] == {
        "title": "US OFAC Specially Designated Nationals",
        "publisher": "Office of Foreign Assets Control",
        "official": True,
    }
    assert "official" not in records["sanctions"]  # a collection has no publisher


def test_freshness_reports_the_default_collection(load_fixture) -> None:
    catalog = DatasetsResponse.model_validate(load_fixture("catalog"))
    fresh = freshness(catalog)
    assert fresh is not None
    assert fresh["version"] == catalog.datasets[0].index_version
    assert fresh["updated_at"].startswith("2026-05-30")
    assert freshness(DatasetsResponse(datasets=[])) is None


def test_feature_descriptions_follow_the_default_alias(load_fixture) -> None:
    resp = AlgorithmsResponse.model_validate(load_fixture("algorithms"))
    features = feature_descriptions(resp)
    assert features["name_match"].startswith("Match two entities")
    assert "dob_year_disjoint" in features


def test_unknown_name_suggests_entries_containing_the_query() -> None:
    titles = {
        "us_ofac_sdn": "OFAC Specially Designated Nationals",
        "us_ofac_cons": "OFAC Consolidated List",
        "eu_fsf": "EU Financial Sanctions Files",
    }
    message = unknown_name("dataset", "ofac", titles)
    assert message.startswith("Unknown dataset 'ofac'. Closest: ")
    assert "us_ofac_sdn (OFAC Specially Designated Nationals)" in message
    assert "us_ofac_cons" in message
    assert "eu_fsf" not in message


def test_unknown_name_falls_back_to_fuzzy_matches() -> None:
    message = unknown_name("program", "US-RUSHARM", {"US-RUSHAR": None, "EU-UKR": "Ukraine"})
    assert message == "Unknown program 'US-RUSHARM'. Closest: US-RUSHAR."
    assert unknown_name("program", "zzz", {"EU-UKR": "Ukraine"}).endswith("nothing close to it.")
