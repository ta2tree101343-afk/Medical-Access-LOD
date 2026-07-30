# Infra (AWS CDK, TypeScript)

Medical Access LOD の AWS サーバーレス基盤。設計書 §19 準拠で 6 Stack に分割。

## Stack 構成

| Stack | 内容 |
| --- | --- |
| `Storage` | S3 (raw / normalized / build) + DynamoDB (読取モデル) + ECR |
| `Delivery` | S3 (dist) + CloudFront (OAC) + アクセスログバケット |
| `Pipeline` | Lambda x7 (Docker image, arm64) + Step Functions + EventBridge Scheduler (biannual + cleanup-rescan 24h) + SQS + Cleanup DLQ |
| `Api` | API Gateway HTTP API + Lambda (Python 3.12, arm64) |
| `Monitoring` | CloudWatch alarms x17 + SNS Topic + Dashboard + AWS Budgets (任意) |
| `Identity` | GitHub OIDC Provider + Deploy Role (最小権限) |

## 前提 (共通)

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
npm run build       # tsc
npm run synth       # 全 Stack を CloudFormation テンプレートに合成
npm test            # jest でユニットテスト (40 tests)
npx cdk diff        # 現行環境との差分
npx cdk deploy --all --require-approval never  # デプロイ
```

---

## Runbook: 初回デプロイ (dev)

**AWS 環境が空の状態から動作させるまでの完全手順。stg / prod は「dev で使った context を書き換えて再実行」で足りる。**

### 前提

- AWS アカウントの `AdministratorAccess` を持つ IAM ユーザまたはロールで `aws configure` 済み
- Node.js 22, Python 3.12, uv, Docker がインストール済み
- 対象リージョンは `ap-northeast-1` (東京) 固定

### Step 1: CDK Bootstrap (アカウント x リージョン ごとに 1 回だけ)

```bash
npx cdk bootstrap aws://<AWS_ACCOUNT_ID>/ap-northeast-1
```

CDK 用の staging bucket / IAM ロール群が作成される。所要 2 分。

### Step 2: Identity Stack を先にローカルからデプロイ (鶏卵問題の解決)

GitHub Actions の deploy.yml は OIDC で `GithubDeployRole` を assume する必要があるが、
そのロールを作るのがまさにこのスタック。よって初回だけローカル (AdministratorAccess)
から個別にデプロイする。

```bash
cd infra
npm ci
npm run build
npx cdk deploy MedicalAccessLod-dev-Identity \
  --require-approval never \
  -c env=dev \
  -c githubOwner=<YOUR_GH_LOGIN> \
  -c githubRepo=Medical-Access-LOD
```

**Outputs で `DeployRoleArn` と `OidcProviderArn` が表示される。この 2 つを控える。**

### Step 3: GitHub Secrets を登録

Repo → Settings → Environments → **`dev`** (存在しなければ作成) に以下を Secret として登録:

| 名前 | 値 |
| --- | --- |
| `AWS_ACCOUNT_ID` | 12 桁の AWS アカウント ID |
| `AWS_DEPLOY_ROLE_ARN` | Step 2 の `DeployRoleArn` |
| `CLOUDFRONT_DISTRIBUTION_ID` | (未作成なら空。DeliveryStack デプロイ後に登録) |

### Step 4: 残り 5 Stack を GitHub Actions からデプロイ

GitHub Repo → Actions → **Deploy** → Run workflow:

- `env`: `dev`
- `snapshot_date`: `2025-12-01`
- `source_url`: `https://data.e-gov.go.jp/data/dataset/321fdf20-5f6a-49e5-bcab-35d81d652c65/resource/af88450b-049c-4deb-8dc9-327312d877e1/download/e-gov20251201.zip`

Workflow が以下を順に実行:

1. OIDC で Role assume
2. Docker build (linux/arm64) → ECR に **commit SHA タグ** で push (+ `latest` も併記)
3. `cdk deploy --all` で残り 5 Stack (`Storage / Delivery / Pipeline / Api / Monitoring`) をデプロイ
4. `lod/` 配下を dist バケットの `latest/` へ `aws s3 sync` する。ただし
   `manifest.json` (Publish Lambda が世代 commit 用に管理) と `*.md` は除外
5. CloudFront invalidation (`/latest/*`)

所要 8〜12 分。

### Step 5: 動作確認 (スモークテスト)

以下は `smoke.yml` workflow が自動で叩くが、初回は手動確認しておくと安全:

```bash
# CloudFront ドメインを取得
DIST=$(aws cloudformation describe-stacks --stack-name MedicalAccessLod-dev-Delivery \
  --query 'Stacks[0].Outputs[?OutputKey==`DistributionDomain`].OutputValue' --output text)

# 公開 LOD の manifest が乗っているか
curl -sSfI "https://$DIST/latest/manifest.json"
curl -sSfI "https://$DIST/latest/medical-access-lod.ttl"

# API GW URL を取得
API=$(aws cloudformation describe-stacks --stack-name MedicalAccessLod-dev-Api \
  --query 'Stacks[0].Outputs[?OutputKey==`HttpApiUrl`].OutputValue' --output text)

# ヘルスチェック (Read Model はまだ空なので count=0 でも OK)
curl -sSf "$API/health"
curl -sSf "$API/metadata"
```

### Step 6: (任意) 初回パイプラインを手動起動

Scheduler は `Asia/Tokyo` の 6/1・12/1 00:00 に自動起動するが、初回は待たずに実行できる:

```bash
aws stepfunctions start-execution \
  --state-machine-arn $(aws cloudformation describe-stacks \
    --stack-name MedicalAccessLod-dev-Pipeline \
    --query 'Stacks[0].Outputs[?OutputKey==`StateMachineArn`].OutputValue' --output text) \
  --input '{"snapshot_date":"2025-12-01","source_url":"https://data.e-gov.go.jp/data/dataset/321fdf20-5f6a-49e5-bcab-35d81d652c65/resource/af88450b-049c-4deb-8dc9-327312d877e1/download/e-gov20251201.zip"}'
```

Step Functions コンソールで進捗確認 (Download → Normalize → BuildRdf → Validate → BuildReadModel → Publish → Cleanup)。所要 5〜10 分。

### Step 7: (任意) Budget 通知の有効化

未指定なら Budget は作成されない。有効化するには CDK context に email を渡す:

```bash
npx cdk deploy MedicalAccessLod-dev-Monitoring \
  -c budgetEmail=you@example.com \
  -c monthlyBudgetUsd=10
```

80% 到達で ACTUAL 通知、100% forecast 到達で FORECASTED 通知。

**⚠ 初回のみ手動確認が必要**: `budgetEmail` に指定したアドレスに AWS から
「Confirm subscription」メールが届く。**リンクをクリックして購読を確定するまで
アラートは配信されない** (deploy は成功する)。運用開始前に必ず確定操作を行うこと。
また AWS Budgets 自体を有効化していないアカウント (billing プリファレンス未設定)
では deploy が汎用 CFN エラーで失敗する — Billing コンソールで有効化してから再実行。

---

## 想定料金 (dev 環境、月次)

| リソース | 前提 | 概算 (USD) |
| --- | --- | --- |
| Lambda | パイプライン 6 Lambda × 2 回/年 (半期スナップショット) = 12 invocation + Cleanup rescan 1/日 = 30 invocation/月。合計 数十 invocation/月 | ~$0.01 |
| Step Functions | 2 回/年 実行 | ~$0.00 |
| DynamoDB (on-demand) | 読取モデル書き込み ~30K/回 x 2 + API 検索 (1K/月) | ~$0.05 |
| S3 (raw + build + dist) | ~50 MB 保管 + `latest/` GET (1K/月) | ~$0.02 |
| CloudFront | 100 GB egress/月 (公開 LOD ダウンロード想定) | ~$8.50 |
| ECR | Lambda コンテナイメージ ~800 MB | ~$0.08 |
| CloudWatch (logs / metrics / alarms) | Logs 1 GB/月 + alarms 17 個 | ~$0.85 |
| X-Ray | traces 100K/月 | ~$0.50 |

**合計月額 $10 前後**。CloudFront egress が支配的なので、実際のダウンロード量次第で変動する。Budget を月 $10 に設定するのは妥当。

---

## stg / prod への横展開

`dev` で動作確認後、以下だけを変えて Step 2 以降を再実行:

- Environment を `stg` / `prod` として作成し Secrets を分離
- `cdk deploy` の `-c env=stg` / `-c env=prod` に変更
- Stack 名は `MedicalAccessLod-stg-*` / `MedicalAccessLod-prod-*` に自動で切り替わる

**OIDC Provider は AWS アカウント全体で 1 つ**なので、既に IdentityStack を dev で作成済みなら、stg/prod では OIDC provider の重複作成が失敗する。以下いずれかで対応:

- (推奨) IdentityStack のみ dev で 1 度作り、stg/prod では Deploy Role だけ別途作る (現状の CDK は 1 stack で両方作るため、stg/prod では OIDC provider を `imported` に差し替える改修が必要 — 未実装、TODO)
- (暫定) stg/prod でも同じ Role を共用する場合、`githubRepo` の trust condition を stg/prod で分けなくて済むよう wildcards を許容する

---

## Rollback

### Case A: CDK Deploy が失敗した場合

CloudFormation が自動で前バージョンに戻す (ROLLBACK_COMPLETE)。手動対応不要。

### Case B: Deploy 成功したが Lambda 実装が不良

```bash
# 直前の SHA を確認
git log --oneline -5

# その SHA を imageTag として再 deploy
gh workflow run deploy.yml \
  -f env=dev \
  -f snapshot_date=2025-12-01 \
  -f source_url=...
# workflow_run はブランチ HEAD を build するので、事前に問題コミットを revert してから push
```

### Case C: dist bucket に破損した LOD を配信中

```bash
# CloudFront で緊急 invalidation
aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths '/latest/*'

# S3 の versioning から前バージョンを復旧
aws s3api list-object-versions --bucket "$DIST_BUCKET" --prefix "latest/"
# Get previous version ID and copy over
```

### Case D: DynamoDB の Read Model が破損

- 過去の catalog エントリを list: `aws dynamodb query --table-name ... --key-condition-expression "PK = :pk" --expression-attribute-values '{":pk":{"S":"SYSTEM#GENERATION"}}'`
- 健全な COMMITTED 世代の `run_id` を控える
- `latest/manifest.json` を書き換えてその世代へ差し戻す (要 ETag CAS 手動オペレーション)

---

## 破棄

課金停止のため全リソースを削除する場合:

```bash
# API / Monitoring / Pipeline は自動 rollback で消える
npx cdk destroy MedicalAccessLod-dev-Api MedicalAccessLod-dev-Monitoring MedicalAccessLod-dev-Pipeline

# Delivery は CloudFront が絡むので削除に 20 分程度かかる
npx cdk destroy MedicalAccessLod-dev-Delivery

# Storage は S3 / DynamoDB / ECR にデータが残っているため、
# `removalPolicy: DESTROY` + `autoDeleteObjects: true` が付いていないバケットは
# 事前に empty してから
aws s3 rm s3://medical-access-lod-dev-raw --recursive
aws s3 rm s3://medical-access-lod-dev-normalized --recursive
aws s3 rm s3://medical-access-lod-dev-build --recursive
aws s3 rm s3://medical-access-lod-dev-dist --recursive
aws ecr batch-delete-image --repository-name medical-access-lod-dev-pipeline --image-ids "$(aws ecr list-images --repository-name medical-access-lod-dev-pipeline --query 'imageIds' --output json)"
npx cdk destroy MedicalAccessLod-dev-Storage

# OIDC Provider は他プロジェクトが共有している可能性があるため最後
npx cdk destroy MedicalAccessLod-dev-Identity
```

---

## Context

- `env` (default: `dev`) — Stack 名プレフィックスに使用
- `githubOwner` / `githubRepo` — Identity Stack の OIDC 信頼条件に使用
- `snapshotDate` — Scheduler が処理するスナップショット日 (`YYYY-MM-DD`)
- `sourceUrl` — 同スナップショットの HTTPS ZIP URL (URL 内に `YYYYMMDD` 必須)
- `imageTag` — Lambda コンテナ image tag (通常は commit SHA)、ローカルでは省略可 (`latest` に fallback)
- `budgetEmail` — 月次 Budget 通知先。未指定なら Budget は作成しない
- `monthlyBudgetUsd` — Budget 閾値 (default 10)

`snapshotDate` と `sourceUrl` は必ず同時に更新する。CDK synth 時に日付の実在性、
HTTPS、URL 内の日付一致を検証するため、設定の片方だけを変えると合成が失敗する。

```bash
npx cdk deploy --all \
  -c env=prod \
  -c snapshotDate=2026-06-01 \
  -c sourceUrl=https://data.example/e-gov20260601.zip
```

Scheduler は `Asia/Tokyo` の 6 月 1 日・12 月 1 日 **00:00 JST** に起動する。

---

## セキュリティ方針

- 全 S3 バケット: `BlockPublicAccess.BLOCK_ALL` + SSE + `enforceSSL: true`
- CloudFront: OAC 経由のみ S3 参照可、TLS 1.2+、`SECURITY_HEADERS` 適用
- Lambda: arm64、X-Ray アクティブトレース、環境変数で構造化ログ設定
- IAM: 各 Lambda に必要な最小権限、Cleanup は SYSTEM#PIPELINE lock に触れない
- GitHub Actions からのデプロイは長期アクセスキーを保持せず OIDC で一時認証
- OIDC Deploy Role は `latest/*` prefix のみ write 可能。`releases/` / `archives/` は改変不可
- DynamoDB: PITR 有効、削除保護
- ECR: プッシュ時スキャン有効
- **cdk-nag** の AwsSolutionsChecks を全 stack に適用、意図的な緩和は `bin/medical-access-lod.ts` の `NagSuppressions` で明示

## パイプラインの公開整合性

- `BuildReadModel` は期限付き DynamoDB lock を取得し、世代別キーへ書き込む
- `Publish` は `releases/<snapshot_date>/<encoded_run_id>/` へ 2 形式を配置する
- `latest/manifest.json` の ETag 条件付き単一 PutObject を公開世代の commit point とする
- API は manifest の `run_id` が指す DynamoDB 世代だけを検索する
- Publish 成功・失敗のどちらでも所有者条件付きで lock を解放し、異常終了で解放できない
  場合も lease 期限後に後続実行が取得できる
- 旧世代 GC (Cleanup Lambda) は SQS 経由 + 24h 間隔の EventBridge 再走査で二重化

## テスト (CDK Assertions + ASL E2E)

- S3 全バケットの public access ブロック / SSE
- DynamoDB GSI (CityBySpecialty / SpecialtyByDay) の存在
- ECR の scan-on-push
- Lambda 7 個・arm64・ACTIVE tracing
- Step Functions の tracing enabled
- EventBridge Scheduler の cron 式と TZ、cleanup rescan schedule (24h)
- Scheduler 入力の日付・URL 整合性と全 Lambda イベント契約
- ReadModel → 不変 release → manifest commit の実 ASL 連鎖
- HTTP API のプロトコルとルート
- CloudFront の HTTPS 強制と OAC
- SNS Topic + CloudWatch alarms 17 件 (含 Cleanup errors / DLQ)
- **Lambda Code.ImageUri が imageTag context を伝搬すること (SHA タグ propagation の pin)**
- **OIDC Deploy Role の S3 write が `latest/*` 限定であること**
- GitHub OIDC provider + Deploy Role の信頼条件

## ノート

- Lambda 関数のイメージは `PipelineStack` が ECR から取得する前提。deploy.yml が
  `docker build` → `aws ecr get-login-password` → `docker push` を自動で行う
- 実装は本リポジトリの `src/medical_access_lod/functions/*`
- 冪等性は Powertools Idempotency ではなく domain-level (SHA-256 caching, manifest CAS,
  pipeline lock, deletion cursor) で保証。詳細は
  `src/medical_access_lod/functions/shared/pipeline_lock.py` のコメント参照

---

## Troubleshooting: 実デプロイで踏んだ罠 (2026-07-30 記録)

初回 dev デプロイで遭遇し、修正した issue 集。stg/prod 展開時や、破棄後の再構築で
同じ罠を踏まないためのメモ。順序は「先に発生する」→「後に発生する」の実行順。

### 1. OIDC AssumeRoleWithWebIdentity が 12 回リトライして AccessDenied

**症状**: `aws-actions/configure-aws-credentials` が `Assuming role with OIDC` を
12 回繰り返し、最後に `Error: Could not assume role with OIDC: Not authorized to
perform sts:AssumeRoleWithWebIdentity`。CloudTrail の event log で JWT の
`userIdentity.principalId` を見ると、subject が
`repo:OWNER@OWNERID/REPO@REPOID:environment:ENV` の形をしている。

**原因**: GitHub が **immutable ID 付きの subject claim** を発行するアカウント
(セキュリティ強化 org / enterprise) では、trust condition の `repo:OWNER/REPO:*`
パターンが `@OWNERID` の位置で不一致する。`*` は `/` の直前まで貪欲マッチしないため。

**修正**: `infra/lib/identity-stack.ts` の trust condition を
`repo:${owner}*/${repo}*:*` に。両形式にマッチする。CDK ソース済み。

**予防**: CloudTrail の `AssumeRoleWithWebIdentity` イベントを見れば必ず判明する。

### 2. Docker build 中に `exec /bin/sh: exec format error`

**症状**: `docker build --platform linux/arm64` の RUN 段 (`pip install` 等) で
`exec format error` が出て非ゼロ終了。base image (arm64) の pull は成功している。

**原因**: GitHub Actions の `ubuntu-latest` runner は amd64。cross-arch build には
QEMU emulation の binfmt_misc 登録が必要。`docker build --platform` だけでは足りない。

**修正**: `deploy.yml` に以下を追加 (build ステップの前):

```yaml
- uses: docker/setup-qemu-action@... with: { platforms: arm64 }
- uses: docker/setup-buildx-action@...
```

かつ build コマンドを `docker buildx build --push` に変更 (buildx builder は
daemon store に image を load しないため、通常の `docker push` と組み合わせられない)。

### 3. Lambda 作成が「The image manifest, config or layer media type is not supported」

**症状**: CFN の `AWS::Lambda::Function` CREATE_FAILED。ECR には image が push
されている。

**原因**: `docker buildx build --push` は既定で **OCI manifest 形式 + provenance
attestation** を作る。Lambda は **Docker Image Manifest V2 Schema 2** のみ受理し、
OCI attestation を拒否する。

**修正**: buildx コマンドに `--provenance=false --sbom=false` を追加。単一 manifest
が push され Lambda が受理する。

### 4. IAM Role 作成が「Value at 'description' failed to satisfy constraint」

**症状**: `AWS::IAM::Role` CREATE_FAILED。正規表現エラーとして
`[\u0009\u000A\u000D\u0020-\u007E\u00A1-\u00FF]*` に一致しないと言われる。
 -~¡-ÿ]*` に一致しないと言われる。

**原因**: IAM Role / Policy の `description` は **ASCII + Latin-1 のみ** (`¡`
= 161)。日本語 (CJK, 0x3000 以上) は完全に範囲外。EventBridge Scheduler の
description も同じ制約。

**修正**: CDK コード内の `description` を英語のみに。今回は `CleanupRescanRole` と
`CleanupRescanSchedule` の 2 箇所を修正済み。

**予防**: `grep -rn "description:.*[^\x00-\x7F]" infra/lib/` で CDK 全体を検査可能。

### 5. Validate Lambda が `/var/ontology/shapes.ttl` を探しに行って FileNotFoundError

**症状**: Step Functions の ValidateTask で `FileNotFoundError: [Errno 2]
No such file or directory: '/var/ontology/shapes.ttl'`。ローカルの pytest では通る。

**原因**: `src/medical_access_lod/infrastructure/rdf/shacl_validator.py` の
`SHAPES_PATH = Path(__file__).resolve().parents[3].parent / "ontology" / "shapes.ttl"`
はローカル (`<repo>/src/...`) 前提。Lambda では `src/` が剥がされて
`/var/task/medical_access_lod/...` に配置されるため、`parents[3].parent` が
`/var/` に化ける。

**修正**: Lambda ランタイムが自動注入する `LAMBDA_TASK_ROOT` env を優先し、
`/var/task/ontology/shapes.ttl` を返す実装に。既に反映済み。

**予防**: 同じパターンで `Path(__file__).parents[N]` を絶対パスの根拠にしている
コードは Lambda で必ず化ける。基本 `LAMBDA_TASK_ROOT` + 相対パスに寄せる。

### 6. Smoke test で `cloudformation:DescribeStacks` AccessDenied

**症状**: deploy.yml の smoke test ステップで `aws cloudformation describe-stacks
--stack-name MedicalAccessLod-dev-Api` が AccessDenied。

**原因**: Deploy Role は CDK bootstrap role 経由で deploy するため、CDK 内部の
API はそのロールが呼び出しており、Deploy Role 自体には CFN read 権限を渡していなかった。

**修正**: `identity-stack.ts` の Deploy Role に read-only 権限を追加:

- `cloudformation:DescribeStacks` `cloudformation:ListStacks` on `stack/MedicalAccessLod-*`
- `cloudfront:GetDistribution` on distribution ARN (`wait distribution-deployed` 用)

### 7. 初回 CloudFront が 15-20 分伝播せず curl 失敗

**症状**: 初回デプロイの smoke test で `curl https://<domain>/latest/*.ttl` が
`--retry 5 --retry-delay 15` (~75 秒) では時間切れ。CloudFront コンソールで
Distribution が **Deploying** 状態のまま。

**原因**: 新規 CloudFront Distribution は edge 全世界への propagation に
15〜20 分かかる。curl retry では追いつけない。

**修正**: smoke curl の前に `aws cloudfront wait distribution-deployed
--id "$DIST_ID"` を挟む。2 回目以降のデプロイでは即座に返る。

---

## デプロイ検証記録 (2026-07-30 dev 環境)

上記 7 件を全て修正した状態で end-to-end 検証済み:

- ✅ CDK deploy → 6 stack CREATE/UPDATE_COMPLETE
- ✅ Docker build → ECR push (SHA タグ + latest)
- ✅ `aws s3 sync lod/` → dist bucket に公開成果物
- ✅ CloudFront invalidation
- ✅ Smoke test: `/health` 200 / `/metadata` 200 / dist bucket HEAD 200
- ✅ Step Functions 手動起動: 85 秒で SUCCEEDED (Download → Normalize → BuildRdf → Validate → BuildReadModel → Publish → NotifyCleanup)
- ✅ 実 API: `/facilities?city=千葉市中央区&specialty=1001` → 71 件、`day=MON&time=18:00` → 10 件
- ✅ DynamoDB: 25,278 items COMMITTED (世代 catalog + facility metadata + service + schedule)
- ✅ manifest.json commit: `releases/2025-12-01/<run_id>/medical-access-lod.jsonld` (17MB)
