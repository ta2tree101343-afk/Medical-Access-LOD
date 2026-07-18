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

- Actions → **Deploy** → **Run workflow** → 環境選択 (`dev` 等)
- 動作:
  1. OIDC で Role assume
  2. Docker build (linux/arm64) → ECR に 6 タグで push
  3. `cdk deploy --all` (残り 5 Stack)
  4. `aws s3 sync lod/ → s3://medical-access-lod-<env>-dist/latest/`
  5. CloudFront invalidation

## Context

- `env` (default: `dev`) — Stack 名プレフィックスに使用
- `githubOwner` / `githubRepo` — Identity Stack の OIDC 信頼条件に使用

例: `npx cdk deploy --all -c env=stg -c githubOwner=my-org`

## セキュリティ方針

- 全 S3 バケット: `BlockPublicAccess.BLOCK_ALL` + SSE + `enforceSSL: true`
- CloudFront: OAC 経由のみ S3 参照可、TLS 1.2+、`SECURITY_HEADERS` 適用
- Lambda: arm64、X-Ray アクティブトレース、環境変数で構造化ログ設定
- IAM: 各 Lambda に必要な最小権限（バケット別 read/write を細分化）
- GitHub Actions からのデプロイは長期アクセスキーを保持せず OIDC で一時認証
- DynamoDB: PITR 有効、削除保護
- ECR: プッシュ時スキャン有効

## テスト（14 件、CDK Assertions）

- S3 全バケットの public access ブロック
- S3 全バケットの SSE
- DynamoDB GSI (CityBySpecialty / SpecialtyByDay) の存在
- ECR の scan-on-push
- Lambda 6 個・arm64・ACTIVE tracing
- Step Functions の tracing enabled
- EventBridge Scheduler の cron 式と TZ
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
