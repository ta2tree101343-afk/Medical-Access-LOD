# Medical Access LOD

[![CI](https://github.com/ta2tree101343-afk/Medical-Access-LOD/actions/workflows/ci.yml/badge.svg)](https://github.com/ta2tree101343-afk/Medical-Access-LOD/actions/workflows/ci.yml)

千葉市の医療機関について、**診療科 × 診療時間 × 所在地**を統合した LOD（Linked Open Data）。厚生労働省 医療情報ネットのオープンデータ (PDL 1.0) を出典とし、`ex:ClinicalService` 中間ノードで「施設 × 診療科 × 診療時間」の 3 項関係を表現する。

- 出典: [厚生労働省 医療情報ネットのオープンデータ](https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/kenkou_iryou/iryou/newpage_43373.html) (PDL 1.0)
- 対象地域: 千葉市（中央区・花見川区・稲毛区・若葉区・緑区・美浜区）
- スナップショット: 2025-12-01

## 公開 LOD (千葉市6区、74,203 トリプル)

```text
https://raw.githubusercontent.com/ta2tree101343-afk/Medical-Access-LOD/main/lod/medical-access-lod.ttl     (4.8 MB, Turtle)
https://raw.githubusercontent.com/ta2tree101343-afk/Medical-Access-LOD/main/lod/medical-access-lod.jsonld  (9.2 MB, JSON-LD)
https://raw.githubusercontent.com/ta2tree101343-afk/Medical-Access-LOD/main/lod/ontology.ttl               (オントロジー + SKOS 診療科スキーム)
https://raw.githubusercontent.com/ta2tree101343-afk/Medical-Access-LOD/main/lod/shapes.ttl                 (SHACL Shapes)
https://raw.githubusercontent.com/ta2tree101343-afk/Medical-Access-LOD/main/lod/statistics.json            (件数統計 + SPARQL 例)
```

詳細と SPARQL 例は [`lod/README.md`](lod/README.md) 参照。

## 統計

| 指標 | 値 |
| --- | --- |
| 医療機関 | 509 (病院 40 / 診療所 469) |
| 診療サービス (施設 × 診療科) | 1,636 |
| 診療時間スロット | 12,978 |
| 診療科 (SKOS Concept, 全ラベル付き) | 82 |
| RDF トリプル | **74,203** |
| SHACL 検証 | 適合 |

## モデル要点

- **中間ノード** `ex:ClinicalService`：施設と診療科・診療時間を橋渡し（二項関係を超えた表現）
- **SKOS 診療科スキーム** `<concept/specialty>`：`skos:Concept` + `skos:notation` (公式 4 桁コード) + `skos:prefLabel@ja`
- **具象クラス** `schema:Hospital` / `schema:MedicalClinic`（SHACL `sh:targetClass` は具象を指定）
- **時刻** `xsd:time`（`HH:MM:SS`、24 時制、TZ・小数秒なし）→ SPARQL は `STR()` 比較で移植性を確保
- **URI 設計**：`@base` + 相対 IRI（例：`<resource/facility/1210000123>`）、`ex:` は語彙専用

## 主な SPARQL 例

```sparql
# 千葉市中央区で平日18時以降に受診できる皮膚科 — 43 件
BASE <https://example.org/medical-access/>
PREFIX ex: <https://example.org/medical-access/>
PREFIX schema: <https://schema.org/>
SELECT DISTINCT ?facility ?name ?dayOfWeek ?opens ?closes WHERE {
  VALUES ?dayOfWeek { schema:Monday schema:Tuesday schema:Wednesday schema:Thursday schema:Friday }
  ?facility schema:name ?name ; schema:address ?address ; ex:offersClinicalService ?service .
  ?address schema:addressLocality "千葉市中央区"@ja .
  ?service ex:medicalSpecialty <concept/specialty/6001> ; ex:hasSchedule ?schedule .
  ?schedule schema:dayOfWeek ?dayOfWeek ; schema:opens ?opens ; schema:closes ?closes .
  FILTER(STR(?opens) <= "18:00:00" && STR(?closes) > "18:00:00")
}
```

その他: 内科 (265) / 土曜小児科 (103) / 診療科カタログ (82) / ラベル検索 / 区別集計 — 詳細は [`queries-real/`](queries-real/)。

## 再現手順

```bash
# 依存インストール
uv sync

# 実データを取得して公開成果物を再生成
uv run medical-lod download
uv run medical-lod publish-lod
# → lod/ 一式が更新される (Turtle / JSON-LD / statistics.json)

# fixture のみでエンドツーエンド動作を確認
uv run medical-lod pipeline
```

### 個別実行

```bash
uv run medical-lod normalize       # CSV正規化 (dry-run)
uv run medical-lod build           # RDF生成
uv run medical-lod validate        # SHACL検証
uv run medical-lod test-queries    # queries/ を全実行
uv run medical-lod pipeline-real   # 実データ pipeline
```

## 品質ゲート

```bash
uv run ruff check .
uv run mypy src
uv run pytest
```

- pytest 66 件（unit / integration）
- ruff (E/F/W/I/UP/B/SIM/RUF) + mypy `--strict`
- GitHub Actions で自動実行（Python ジョブ + infra ジョブ並列）

## リポジトリ構成

```text
Medical-Access-LOD/
├── src/medical_access_lod/       # Python 実装 (domain / application / infrastructure / functions)
├── ontology/                     # medical-access.ttl (Ontology + SKOS) と shapes.ttl (SHACL)
├── queries/                      # fixture 用 SPARQL 3 本
├── queries-real/                 # 実データ用 SPARQL 6 本
├── data/fixtures/                # 合成ダミー CSV (単体テスト用)
├── lod/                          # 公開成果物 (Turtle / JSON-LD / README)
├── tests/                        # pytest (unit / integration / contract)
├── infra/                        # AWS CDK (TypeScript, 6 Stack)
├── docs/                         # 設計書・実装ステップ・レポート下書き
├── Dockerfile                    # Lambda コンテナ (public.ecr.aws/lambda/python:3.12)
└── .github/workflows/ci.yml      # Ruff / mypy / pytest / cdk synth / jest
```

## AWS 基盤 (`infra/`)

再現可能な公開基盤として AWS CDK (TypeScript) で 6 Stack を定義。詳細は [`infra/README.md`](infra/README.md)。

- Storage (S3 × 3 + DynamoDB + ECR)
- Delivery (S3 dist + CloudFront OAC)
- Pipeline (Lambda × 6 + Step Functions + EventBridge Scheduler)
- Api (API Gateway HTTP + Lambda)
- Monitoring (CloudWatch alarms × 15 + SNS + Dashboard)
- Identity (GitHub OIDC Provider + Deploy Role)

## ライセンス

- **コード**: MIT
- **LOD データ**: PDL 1.0 に基づき再配布。出典「厚生労働省 医療情報ネット」明記のうえ利用可

## 免責事項

本 LOD は公開データを研究・学習目的で構造化したもの。実際の診療日時を保証しない。臨時休診等は反映できないため、受診前に医療機関へ直接確認すること。医療判断・診断・緊急案内には利用しない。
