# Medical Access LOD — 公開成果物

千葉市（中央区・花見川区・稲毛区・若葉区・緑区・美浜区）の医療機関について、
**診療科 × 診療時間 × 所在地** を統合した LOD（Linked Open Data）。

## 収録内容

| ファイル | 説明 |
| --- | --- |
| `medical-access-lod.ttl` | Turtle 形式（4.8 MB） |
| `medical-access-lod.jsonld` | JSON-LD 形式（9.2 MB） |
| `ontology.ttl` | 独自オントロジー + SKOS 診療科スキーム |
| `shapes.ttl` | SHACL Shapes（適合を確認済み） |
| `statistics.json` | 件数統計と SPARQL 例のヒット数 |

## 出典・ライセンス

- **出典**: 厚生労働省 医療情報ネット
  <https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/kenkou_iryou/iryou/newpage_43373.html>
- **原データライセンス**: 公共データ利用規約 (PDL 1.0)
- **原データスナップショット日**: 2025-12-01
- **原 ZIP の SHA-256**: `56aeff58f69bdf0b5063b7797577a27ae7901ceb02d58a9c420f5ac4f7fd88c3`
- **本 LOD の再配布**: PDL 1.0 に従う。出典として「厚生労働省 医療情報ネット」を明記のうえ再利用可。

## 統計（2025-12-01 snapshot）

| 指標 | 値 |
| --- | --- |
| RDF トリプル | 74,121 |
| 医療機関 | 509（病院 40 / 診療所 469） |
| 診療サービス（施設 × 診療科） | 1,636 |
| 診療時間スロット | 12,978 |

## ダウンロード URL

```text
https://raw.githubusercontent.com/ta2tree101343-afk/Medical-Access-LOD/main/lod/medical-access-lod.ttl
https://raw.githubusercontent.com/ta2tree101343-afk/Medical-Access-LOD/main/lod/medical-access-lod.jsonld
https://raw.githubusercontent.com/ta2tree101343-afk/Medical-Access-LOD/main/lod/ontology.ttl
https://raw.githubusercontent.com/ta2tree101343-afk/Medical-Access-LOD/main/lod/shapes.ttl
```

## モデル概要

- 中間ノード `ex:ClinicalService` により「施設 × 診療科 × 診療時間」の 3 項関係を表現
- 診療科は `skos:ConceptScheme <concept/specialty>` 配下の `skos:Concept`
- 診療科コードは MHLW 公式 4 桁体系（内科=`1001` / 小児科=`3001` / 皮膚科=`6001` 等）を `skos:notation` に採用
- 施設は具象クラス `schema:Hospital` / `schema:MedicalClinic` として型付け
- 時刻は `xsd:time`（`HH:MM:SS`、24 時制、TZ・小数秒なし）

## SPARQL 例（RDFLib での確認済み件数）

```sparql
# 内科（1001）を提供する医療機関 — 265 件
BASE <https://example.org/medical-access/>
PREFIX ex: <https://example.org/medical-access/>
PREFIX schema: <https://schema.org/>
SELECT ?facility ?name WHERE {
  ?facility ex:offersClinicalService ?service ;
            schema:name ?name .
  ?service ex:medicalSpecialty <concept/specialty/1001> .
}
ORDER BY ?name
```

```sparql
# 千葉市中央区で平日18時以降に受診できる皮膚科 — 43 件
# 時刻比較は STR() で行う（RDFLib は xsd:time の順序比較を実装しないため）
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

## 免責事項

本 LOD は公開データを研究・学習目的で構造化したものであり、実際の診療日時を保証しない。
臨時休診等は反映できないため、受診前に医療機関へ直接確認すること。
医療判断、診断、緊急時の案内には利用しない。

## 再生成手順

```bash
uv sync
uv run medical-lod download
uv run medical-lod publish-lod
```
