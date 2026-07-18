"""Typer CLI: uv run medical-lod <subcommand>"""

from __future__ import annotations

from pathlib import Path

import typer
from rdflib import Graph

from medical_access_lod.application.build_rdf import build_rdf
from medical_access_lod.application.download_source import (
    DEFAULT_SNAPSHOT_DATE,
    DEFAULT_SOURCE_URL,
)
from medical_access_lod.application.download_source import (
    download as download_source,
)
from medical_access_lod.application.normalize_data import normalize
from medical_access_lod.application.normalize_mhlw import normalize_mhlw
from medical_access_lod.application.validate_rdf import validate_turtle

app = typer.Typer(add_completion=False, no_args_is_help=True)

_REPO_ROOT = Path(__file__).resolve().parents[2]

_DEFAULT_FIXTURES = _REPO_ROOT / "data" / "fixtures"

_DEFAULT_RAW = _REPO_ROOT / "data" / "raw"

_DEFAULT_OUT = _REPO_ROOT / "data" / "build" / "latest"

_DEFAULT_QUERIES = _REPO_ROOT / "queries"

_DEFAULT_QUERIES_REAL = _REPO_ROOT / "queries-real"

_DEFAULT_SHAPES = _REPO_ROOT / "ontology" / "shapes.ttl"


@app.command()
def download(
    source_url: str = typer.Option(DEFAULT_SOURCE_URL, help="医療情報ネット ZIP URL"),
    snapshot_date: str = typer.Option(DEFAULT_SNAPSHOT_DATE, help="スナップショット日 YYYY-MM-DD"),
    dest_root: Path = typer.Option(_DEFAULT_RAW, help="展開先ディレクトリ (data/raw/)"),
) -> None:
    """医療情報ネットのオープンデータ (PDL 1.0) を取得して data/raw/<snapshot_date>/ に展開する。"""

    result = download_source(dest_root, source_url=source_url, snapshot_date=snapshot_date)

    status = "skipped (same sha256)" if result.skipped else "downloaded"

    typer.echo(
        f"[download] {status} sha256={result.sha256[:12]}... files={len(result.extracted_files)}"
    )

    typer.echo(f"[download] raw_dir={result.raw_dir}")

    typer.echo(f"[download] manifest={result.manifest_path}")


@app.command(name="normalize")
def normalize_cmd(
    facilities: Path = typer.Option(_DEFAULT_FIXTURES / "facilities.csv"),
    services: Path = typer.Option(_DEFAULT_FIXTURES / "services.csv"),
    schedules: Path = typer.Option(_DEFAULT_FIXTURES / "schedules.csv"),
) -> None:
    """CSVを正規化 (dry-run: 件数のみ出力)。"""

    ds = normalize(facilities, services, schedules)

    typer.echo(
        f"facilities={len(ds.facilities)} services={len(ds.services)} schedules={len(ds.schedules)}"
    )


@app.command()
def build(
    facilities: Path = typer.Option(_DEFAULT_FIXTURES / "facilities.csv"),
    services: Path = typer.Option(_DEFAULT_FIXTURES / "services.csv"),
    schedules: Path = typer.Option(_DEFAULT_FIXTURES / "schedules.csv"),
    out_dir: Path = typer.Option(_DEFAULT_OUT),
) -> None:
    """Turtle/JSON-LD を生成。"""

    ds = normalize(facilities, services, schedules)

    result = build_rdf(ds, out_dir)

    typer.echo(f"triples={result.triples} ttl={result.turtle_path} jsonld={result.jsonld_path}")


@app.command()
def validate(
    turtle: Path = typer.Option(_DEFAULT_OUT / "medical-access-lod.ttl"),
    shapes: Path = typer.Option(_DEFAULT_SHAPES),
) -> None:
    """SHACL 検証。違反時は exit code 1。"""

    result = validate_turtle(turtle, shapes)

    if result.conforms:
        typer.echo("SHACL: conforms")

    else:
        typer.echo("SHACL: violations")

        typer.echo(result.report_text)

        raise typer.Exit(code=1)


@app.command(name="test-queries")
def test_queries(
    turtle: Path = typer.Option(_DEFAULT_OUT / "medical-access-lod.ttl"),
    queries_dir: Path = typer.Option(_DEFAULT_QUERIES),
) -> None:
    """queries/*.rq を全て実行して件数を表示。"""

    graph = Graph().parse(source=turtle, format="turtle")

    for query_file in sorted(queries_dir.glob("*.rq")):
        results = list(graph.query(query_file.read_text(encoding="utf-8")))

        typer.echo(f"{query_file.name}: {len(results)} row(s)")


@app.command()
def stats(
    turtle: Path = typer.Option(_DEFAULT_OUT / "medical-access-lod.ttl"),
) -> None:
    """トリプル数など基本統計。"""

    graph = Graph().parse(source=turtle, format="turtle")

    typer.echo(f"triples={len(graph)}")


@app.command()
def pipeline(
    prefecture: str = typer.Option("千葉県"),
    city: str = typer.Option("千葉市"),
    facilities: Path = typer.Option(_DEFAULT_FIXTURES / "facilities.csv"),
    services: Path = typer.Option(_DEFAULT_FIXTURES / "services.csv"),
    schedules: Path = typer.Option(_DEFAULT_FIXTURES / "schedules.csv"),
    out_dir: Path = typer.Option(_DEFAULT_OUT),
    shapes: Path = typer.Option(_DEFAULT_SHAPES),
    queries_dir: Path = typer.Option(_DEFAULT_QUERIES),
) -> None:
    """normalize → build → validate → test-queries を一括実行。"""

    typer.echo(f"[pipeline] prefecture={prefecture} city={city}")

    ds = normalize(facilities, services, schedules)

    typer.echo(
        f"[normalize] facilities={len(ds.facilities)} services={len(ds.services)} schedules={len(ds.schedules)}"
    )

    result = build_rdf(ds, out_dir)

    typer.echo(f"[build] triples={result.triples}")

    validation = validate_turtle(result.turtle_path, shapes)

    if not validation.conforms:
        typer.echo("[validate] FAILED")

        typer.echo(validation.report_text)

        raise typer.Exit(code=1)

    typer.echo("[validate] conforms")

    graph = Graph().parse(source=result.turtle_path, format="turtle")

    for query_file in sorted(queries_dir.glob("*.rq")):
        rows = list(graph.query(query_file.read_text(encoding="utf-8")))

        typer.echo(f"[query] {query_file.name}: {len(rows)} row(s)")

    typer.echo("[pipeline] done")


@app.command(name="pipeline-real")
def pipeline_real(
    snapshot_date: str = typer.Option(
        DEFAULT_SNAPSHOT_DATE, help="data/raw/<snapshot_date>/ を入力にする"
    ),
    raw_root: Path = typer.Option(_DEFAULT_RAW),
    out_dir: Path = typer.Option(_DEFAULT_OUT),
    shapes: Path = typer.Option(_DEFAULT_SHAPES),
    queries_dir: Path = typer.Option(_DEFAULT_QUERIES_REAL),
) -> None:
    """MHLW 医療情報ネットの実データ (data/raw/<snapshot_date>/) から千葉市LODを生成する。"""

    raw_dir = raw_root / snapshot_date
    if not raw_dir.exists():
        typer.echo(f"[error] {raw_dir} が存在しません。先に `medical-lod download` を実行してください。")
        raise typer.Exit(code=1)

    typer.echo(f"[pipeline-real] snapshot={snapshot_date} raw={raw_dir}")

    ds = normalize_mhlw(raw_dir)

    typer.echo(
        f"[normalize] facilities={len(ds.facilities)} services={len(ds.services)} schedules={len(ds.schedules)}"
    )

    result = build_rdf(ds, out_dir)

    typer.echo(f"[build] triples={result.triples}")

    validation = validate_turtle(result.turtle_path, shapes)

    if not validation.conforms:
        typer.echo("[validate] FAILED")
        typer.echo(validation.report_text)
        raise typer.Exit(code=1)

    typer.echo("[validate] conforms")

    graph = Graph().parse(source=result.turtle_path, format="turtle")

    for query_file in sorted(queries_dir.glob("*.rq")):
        rows = list(graph.query(query_file.read_text(encoding="utf-8")))
        typer.echo(f"[query] {query_file.name}: {len(rows)} row(s)")

    typer.echo("[pipeline-real] done")


if __name__ == "__main__":
    app()
