# Infra (AWS CDK, TypeScript)

Medical Access LOD の AWS サーバーレス基盤。設計書 §19 準拠で 6 Stack に分割。

## Stack 構成

| Stack | 内容 |
| --- | --- |
| `Storage` | S3 (raw / normalized / build) + DynamoDB (読取モデル) + ECR |
| `Delivery` | S3 (dist) + CloudFront (OAC) + アクセスログバケット |
| `Pipeline` | Lambda x6 (Docker image, arm64) + Step Functions + EventBridge Scheduler |
| `Api` | API Gateway HTTP API + Lambda (Python 3.12, arm64) |
| `Monitoring` | CloudWatch alarms + SNS Topic + Dashboard |
| `Identity` | GitHub OIDC Provider + Deploy Role (最小権限) |

## 前提

- Node.js 22 系推奨（24 でも動作は確認）
- AWS アカウント + `aws configure` 済み or 環境変数
- 初回のみ `cdk bootstrap`

## セットアップ

```bash
cd infra
npm install
```

## 主なコマンド

```bash
npm run synth       # 全 Stack を CloudFormation テンプレートに合成
npm test            # jest でユニットテスト
npx cdk diff        # 現行環境との差分
npx cdk deploy --all --require-approval never  # デプロイ
```

## GitHub Actions からデプロイ

`.github/workflows/deploy.yml` を `workflow_dispatch` で実行すると、
OIDC で IdentityStack の Role を assume してデプロイまで自動化される。

### 初回セットアップ (手動)

1. AWS アカウントで `npx cdk bootstrap aws://<account>/ap-northeast-1`
2. 初回のみ IAM ロール認証で `npx cdk deploy MedicalAccessLod-dev-Identity`
   （GitHub OIDC Provider + Deploy Role を作成）
3. GitHub リポジトリの **Environments** (`dev` / `stg` / `prod`) に以下を登録:
   - `AWS_DEPLOY_ROLE_ARN` (IdentityStack で作成した `GithubDeployRole` の ARN)
   - `AWS_ACCOUNT_ID`
   - `CLOUDFRONT_DISTRIBUTION_ID` (DeliveryStack デプロイ後の Distribution ID、任意)

### 実行

- Actions → **Deploy** → **Run workflow** で環境、`snapshot_date`、対応する
  `source_url` を入力する（後者2項目は必須）
- 動作:
  1. OIDC で Role assume
  2. Docker build (linux/arm64) → ECR に 6 タグで push
  3. `cdk deploy --all` (残り 5 Stack)
  4. `lod/` の静的資産を dist の `latest/` へ同期
     (`latest/manifest.json` はパイプライン専用のため変更・削除しない)
  5. CloudFront invalidation

## Context

- `env` (default: `dev`) — Stack 名プレフィックスに使用
- `githubOwner` / `githubRepo` — Identity Stack の OIDC 信頼条件に使用
- `snapshotDate` — Scheduler が処理するスナップショット日 (`YYYY-MM-DD`)
- `sourceUrl` — 同スナップショットの HTTPS ZIP URL（URL 内に `YYYYMMDD` 必須）

`snapshotDate` と `sourceUrl` は必ず同時に更新する。CDK synth 時に日付の実在性、
HTTPS、URL 内の日付一致を検証するため、設定の片方だけを変えると合成が失敗する。

```bash
npx cdk deploy --all \
  -c env=prod \
  -c snapshotDate=2026-06-01 \
  -c sourceUrl=https://data.example/e-gov20260601.zip
```

ローカルで省略した場合はリポジトリの既定値 (`2025-12-01`) を使用する。
GitHub ActionsのDeploy workflowでは両方が必須である。新しい公表版へ切り替える際は、
上記 context を指定した `cdk diff` でScheduler入力を確認してから同じcontextでdeployする。

Scheduler は `Asia/Tokyo` の6月1日・12月1日 **00:00 JST** に起動する。

## セキュリティ方針

- 全 S3 バケット: `BlockPublicAccess.BLOCK_ALL` + SSE + `enforceSSL: true`
- CloudFront: OAC 経由のみ S3 参照可、TLS 1.2+、`SECURITY_HEADERS` 適用
- Lambda: arm64、X-Ray アクティブトレース、環境変数で構造化ログ設定
- IAM: 各 Lambda に必要な最小権限（バケット別 read/write を細分化）
- GitHub Actions からのデプロイは長期アクセスキーを保持せず OIDC で一時認証
- DynamoDB: PITR 有効、削除保護
- ECR: プッシュ時スキャン有効

## パイプラインの公開整合性

- `BuildReadModel` は期限付きDynamoDB lockを取得し、世代別キーへ書き込む。
- `Publish` は `releases/<snapshot_date>/<encoded_run_id>/` へ2形式を配置する。
- `latest/manifest.json` のETag条件付き単一PutObjectを公開世代のcommit pointとする。
- APIはmanifestの `run_id` が指すDynamoDB世代だけを検索する。
- Publish成功・失敗のどちらでも所有者条件付きでlockを解放し、異常終了で解放できない
  場合もlease期限後に後続実行が取得できる。

## テスト（CDK Assertions + ASL E2E）

- S3 全バケットの public access ブロック
- S3 全バケットの SSE
- DynamoDB GSI (CityBySpecialty / SpecialtyByDay) の存在
- ECR の scan-on-push
- Lambda 6 個・arm64・ACTIVE tracing
- Step Functions の tracing enabled
- EventBridge Scheduler の cron 式と TZ
- Scheduler入力の日付・URL整合性と全Lambdaイベント契約
- ReadModel → 不変release → manifest commit の実ASL連鎖
- HTTP API のプロトコルとルート
- CloudFront の HTTPS 強制と OAC
- SNS Topic + CloudWatch alarms 15 件
- GitHub OIDC provider + Deploy Role の信頼条件

## ノート

- Lambda 関数のイメージは `PipelineStack` が ECR から取得する前提。デプロイ前に
  `docker build` → `aws ecr get-login-password` → `docker push` が必要。
- 実装は本リポジトリの `src/medical_access_lod/functions/*` を想定。
- 現状は「合成できる CDK コード」までの整備。実運用にはハンドラ実装 + 画像 push + 監視通知先の
  SNS サブスクリプション（メール等）追加が必要。
