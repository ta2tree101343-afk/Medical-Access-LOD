# Medical Access LOD

千葉市の医療機関について、**診療科 × 診療時間 × 所在地**を統合したLOD（RDF）。

## 再現手順

```bash
uv sync

uv run medical-lod pipeline --prefecture 千葉県 --city 千葉市

uv run medical-lod download && uv run medical-lod publish-lod

# 個別実行
uv run medical-lod normalize
uv run medical-lod build
uv run medical-lod validate
uv run medical-lod test-queries
```

## テスト

```bash
uv run ruff check .
uv run mypy src
uv run pytest
```

## ステータス

**Phase 3（ローカルETL）まで実装済み**。実データ取得（Phase 1 G-1/G-2）は
`docs/source-and-license.md` 確定後に着手する。fixture でのエンドツーエンド動作は
`data/fixtures/` から実行可能。

## ライセンス

コード: MIT / データ（LOD）: 出典確定後に CC-BY-4.0 予定。
