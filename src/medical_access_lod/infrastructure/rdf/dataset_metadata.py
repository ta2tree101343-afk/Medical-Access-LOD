from __future__ import annotations

from dataclasses import dataclass, field

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, XSD

from medical_access_lod.infrastructure.rdf.uri_factory import BASE, SCHEMA

VOID = Namespace("http://rdfs.org/ns/void#")
DCAT = Namespace("http://www.w3.org/ns/dcat#")
DCT = Namespace("http://purl.org/dc/terms/")
FOAF = Namespace("http://xmlns.com/foaf/0.1/")


@dataclass(frozen=True)
class DatasetMetadata:
    title: str = "Medical Access LOD (千葉市)"
    description: str = (
        "厚生労働省 医療情報ネットのオープンデータ (PDL 1.0) を出典に、"
        "千葉市6区の医療機関について 診療科・診療時間・所在地・位置情報 を "
        "統合した LOD。中間ノード ex:ClinicalService により 施設 x 診療科 x "
        "診療時間 の 3 項関係を表現する。"
    )
    snapshot_date: str | None = None
    generated_at: str | None = None
    source_name: str = "厚生労働省 医療情報ネット"
    source_url: str = (
        "https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/kenkou_iryou/iryou/newpage_43373.html"
    )
    license_url: str = "https://www.digital.go.jp/resources/data/public_data_license"
    license_label: str = "PDL 1.0"
    homepage: str = "https://github.com/ta2tree101343-afk/Medical-Access-LOD"
    dump_ttl_url: str = (
        "https://raw.githubusercontent.com/ta2tree101343-afk/Medical-Access-LOD/main/lod/"
        "medical-access-lod.ttl"
    )
    dump_jsonld_url: str = (
        "https://raw.githubusercontent.com/ta2tree101343-afk/Medical-Access-LOD/main/lod/"
        "medical-access-lod.jsonld"
    )
    keywords: list[str] = field(
        default_factory=lambda: [
            "医療機関",
            "LOD",
            "SKOS",
            "千葉市",
            "SPARQL",
            "healthcare",
            "linked-data",
        ]
    )


def add_dataset_metadata(graph: Graph, metadata: DatasetMetadata | None = None) -> None:
    """グラフに void:Dataset + dcat:Dataset のメタデータを追加する。"""
    if metadata is None:
        metadata = DatasetMetadata()

    graph.bind("void", VOID)
    graph.bind("dcat", DCAT)
    graph.bind("dct", DCT)
    graph.bind("foaf", FOAF)

    ds = URIRef(f"{BASE}dataset")

    graph.add((ds, RDF.type, VOID.Dataset))
    graph.add((ds, RDF.type, DCAT.Dataset))

    graph.add((ds, DCT.title, Literal(metadata.title, lang="ja")))
    graph.add((ds, DCT.description, Literal(metadata.description, lang="ja")))
    graph.add((ds, DCT.language, Literal("ja")))

    if metadata.snapshot_date:
        graph.add((ds, DCT.temporal, Literal(metadata.snapshot_date)))
    if metadata.generated_at:
        graph.add((ds, DCT.issued, Literal(metadata.generated_at, datatype=XSD.dateTime)))

    graph.add((ds, DCT.source, Literal(metadata.source_name, lang="ja")))
    graph.add((ds, DCT.license, URIRef(metadata.license_url)))
    graph.add((ds, DCT.rights, Literal(metadata.license_label)))
    graph.add((ds, FOAF.homepage, URIRef(metadata.homepage)))

    for kw in metadata.keywords:
        graph.add((ds, DCAT.keyword, Literal(kw)))

    # VoID: statistics
    graph.add((ds, VOID.uriSpace, Literal(BASE)))
    for voc in (
        "https://schema.org/",
        "http://www.w3.org/2004/02/skos/core#",
        "https://example.org/medical-access/",
    ):
        graph.add((ds, VOID.vocabulary, URIRef(voc)))
    graph.add((ds, VOID.dataDump, URIRef(metadata.dump_ttl_url)))

    # Entity/class counts (approximate)
    facility_count = 0
    for cls in (SCHEMA.Hospital, SCHEMA.MedicalClinic, SCHEMA.Dentist):
        facility_count += len(list(graph.subjects(RDF.type, cls)))
    graph.add((ds, VOID.entities, Literal(facility_count, datatype=XSD.integer)))
    graph.add(
        (ds, VOID.classes, Literal(len(set(graph.objects(predicate=RDF.type))), datatype=XSD.integer))
    )
    graph.add(
        (ds, VOID.distinctSubjects, Literal(len(set(graph.subjects())), datatype=XSD.integer))
    )

    # DCAT distributions
    for fmt, url, media_type in (
        ("Turtle", metadata.dump_ttl_url, "text/turtle"),
        ("JSON-LD", metadata.dump_jsonld_url, "application/ld+json"),
    ):
        dist = URIRef(f"{BASE}distribution/{fmt.lower()}")
        graph.add((ds, DCAT.distribution, dist))
        graph.add((dist, RDF.type, DCAT.Distribution))
        graph.add((dist, DCT.title, Literal(f"{metadata.title} ({fmt})", lang="ja")))
        graph.add((dist, DCT["format"], Literal(fmt)))
        graph.add((dist, DCAT.mediaType, Literal(media_type)))
        graph.add((dist, DCAT.downloadURL, URIRef(url)))
        graph.add((dist, DCAT.accessURL, URIRef(url)))

    # void:triples: 自己参照になるが、後付けで最終カウントを載せる
    graph.add((ds, VOID.triples, Literal(len(graph) + 1, datatype=XSD.integer)))
