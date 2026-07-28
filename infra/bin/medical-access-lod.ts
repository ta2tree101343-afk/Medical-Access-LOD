#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { AwsSolutionsChecks, NagSuppressions } from 'cdk-nag';
import { StorageStack } from '../lib/storage-stack';
import { PipelineStack } from '../lib/pipeline-stack';
import { ApiStack } from '../lib/api-stack';
import { DeliveryStack } from '../lib/delivery-stack';
import { MonitoringStack } from '../lib/monitoring-stack';
import { IdentityStack } from '../lib/identity-stack';

const app = new cdk.App();

const envName = app.node.tryGetContext('env') ?? 'dev';
const account = process.env.CDK_DEFAULT_ACCOUNT;
const region = process.env.CDK_DEFAULT_REGION ?? 'ap-northeast-1';
const env: cdk.Environment | undefined = account ? { account, region } : undefined;

const githubOwner = app.node.tryGetContext('githubOwner') ?? 'ta2tree101343-afk';
const githubRepo = app.node.tryGetContext('githubRepo') ?? 'Medical-Access-LOD';
const defaultSnapshotDate = '2025-12-01';
const defaultSourceUrl =
  'https://data.e-gov.go.jp/data/dataset/321fdf20-5f6a-49e5-bcab-35d81d652c65' +
  '/resource/af88450b-049c-4deb-8dc9-327312d877e1/download/e-gov20251201.zip';
const snapshotDate = String(app.node.tryGetContext('snapshotDate') ?? defaultSnapshotDate);
const sourceUrl = String(app.node.tryGetContext('sourceUrl') ?? defaultSourceUrl);
// Budget 通知先 (未設定なら Budget は作成しない — 個人検証で誤配信を避けるため)
const budgetEmailRaw = String(app.node.tryGetContext('budgetEmail') ?? '').trim();
const budgetEmail = budgetEmailRaw || undefined;
const monthlyBudgetUsd = Number(app.node.tryGetContext('monthlyBudgetUsd') ?? 10);
// deploy.yml が `-c imageTag=<sha>` で渡す。ローカル `cdk synth` では 'latest' で代用する。
// `??` は空文字列でフォールバックしないため、CI で outputs 未設定になった場合の
// silent-fail を避けるべく `||` を使い、空/空白の場合も 'latest' に落とす。
const rawImageTag = String(app.node.tryGetContext('imageTag') ?? '').trim();
const imageTag = rawImageTag || 'latest';

const prefix = `MedicalAccessLod-${envName}`;

const storage = new StorageStack(app, `${prefix}-Storage`, { env, envName });

const delivery = new DeliveryStack(app, `${prefix}-Delivery`, {
  env,
  envName,
});

const pipeline = new PipelineStack(app, `${prefix}-Pipeline`, {
  env,
  envName,
  snapshotDate,
  sourceUrl,
  rawBucket: storage.rawBucket,
  normalizedBucket: storage.normalizedBucket,
  buildBucket: storage.buildBucket,
  distBucket: delivery.distBucket,
  readModelTable: storage.readModelTable,
  ecrRepository: storage.ecrRepository,
  imageTag,
});

const api = new ApiStack(app, `${prefix}-Api`, {
  env,
  envName,
  readModelTable: storage.readModelTable,
  distBucket: delivery.distBucket,
  ecrRepository: storage.ecrRepository,
  imageTag,
});

const monitoring = new MonitoringStack(app, `${prefix}-Monitoring`, {
  env,
  envName,
  pipelineStateMachine: pipeline.stateMachine,
  apiFunction: api.apiFunction,
  pipelineFunctions: pipeline.pipelineFunctions,
  cleanupFunction: pipeline.cleanupFunction,
  cleanupDlq: pipeline.cleanupDlq,
  budgetEmail,
  monthlyBudgetUsd,
});

const identity = new IdentityStack(app, `${prefix}-Identity`, {
  env,
  envName,
  githubOwner,
  githubRepo,
  ecrRepositoryArn: storage.ecrRepository.repositoryArn,
  distributionArn: delivery.distributionArn,
  distBucket: delivery.distBucket,
});

// cdk-nag: AWS Solutions Checks を全 stack に適用する。
// 個別ルールの緩和はスタック単位の NagSuppressions で明示 (履歴が追える)。
cdk.Aspects.of(app).add(new AwsSolutionsChecks({ verbose: false }));

// 意図的に許容する緩和 (公開 LOD リポジトリの前提)。
// IAM4/IAM5 のような広めのルールは **stack blanket ではなく per-construct** で
// 抑制する。stack blanket にすると意図しない新しい wildcard も透過してしまい
// cdk-nag の意味が薄れる。ここに残す共通は本当に stack 全体に一様な緩和のみ。
const commonSuppressions = [
  { id: 'AwsSolutions-L1', reason: 'Lambda ランタイムはコンテナイメージのため Python 3.12 を Dockerfile で固定。' },
];
for (const stack of [storage, delivery, pipeline, api]) {
  NagSuppressions.addStackSuppressions(stack, commonSuppressions);
}
NagSuppressions.addStackSuppressions(storage, [
  { id: 'AwsSolutions-S1', reason: '内部 raw / normalized / build bucket は S3 server access log を取らずコスト削減。CloudTrail data event で監査可能。' },
]);
NagSuppressions.addStackSuppressions(monitoring, [
  { id: 'AwsSolutions-SNS3', reason: 'アラート通知用 SNS Topic は AWS サービス (CloudWatch) からのみ Publish される。外部 publisher は存在しないため SSL 強制は不要。' },
]);
// IAM4 (Managed Policy): 各 Lambda の service-role は CDK が
// AWSLambdaBasicExecutionRole を自動付与するため許容。
// 対象パス列挙で新規 Lambda 追加時に見落とさないよう明示。
const pipelineLambdaServiceRolePaths = [
  '/MedicalAccessLod-dev-Pipeline/DownloadFunction/ServiceRole/Resource',
  '/MedicalAccessLod-dev-Pipeline/NormalizeFunction/ServiceRole/Resource',
  '/MedicalAccessLod-dev-Pipeline/BuildRdfFunction/ServiceRole/Resource',
  '/MedicalAccessLod-dev-Pipeline/ValidateFunction/ServiceRole/Resource',
  '/MedicalAccessLod-dev-Pipeline/PublishFunction/ServiceRole/Resource',
  '/MedicalAccessLod-dev-Pipeline/BuildReadModelFunction/ServiceRole/Resource',
  '/MedicalAccessLod-dev-Pipeline/CleanupFunction/ServiceRole/Resource',
];
for (const path of pipelineLambdaServiceRolePaths) {
  NagSuppressions.addResourceSuppressionsByPath(pipeline, path, [
    { id: 'AwsSolutions-IAM4', reason: 'AWSLambdaBasicExecutionRole (CDK 自動付与) を許容。他の権限はコード側で明示。' },
  ]);
}
// IAM5 (wildcard): それぞれ理由が違うため個別に。
NagSuppressions.addResourceSuppressionsByPath(
  identity,
  '/MedicalAccessLod-dev-Identity/GithubDeployRole/DefaultPolicy/Resource',
  [
    { id: 'AwsSolutions-IAM5', reason: 'CDK bootstrap role (arn:...:role/cdk-*)・ECR / S3 latest/* の 3 wildcard は運用上不可避。resource は prefix で最小化済み。' },
  ],
);
// pipeline stack の各 Lambda DefaultPolicy は S3 GetObject/PutObject の
// `bucket/prefix/*` wildcard を持つ。範囲は prefix で絞り込み済み。
const pipelineWildcardPaths = [
  '/MedicalAccessLod-dev-Pipeline/DownloadFunction/ServiceRole/DefaultPolicy/Resource',
  '/MedicalAccessLod-dev-Pipeline/NormalizeFunction/ServiceRole/DefaultPolicy/Resource',
  '/MedicalAccessLod-dev-Pipeline/BuildRdfFunction/ServiceRole/DefaultPolicy/Resource',
  '/MedicalAccessLod-dev-Pipeline/ValidateFunction/ServiceRole/DefaultPolicy/Resource',
  '/MedicalAccessLod-dev-Pipeline/PublishFunction/ServiceRole/DefaultPolicy/Resource',
  '/MedicalAccessLod-dev-Pipeline/BuildReadModelFunction/ServiceRole/DefaultPolicy/Resource',
  '/MedicalAccessLod-dev-Pipeline/CleanupFunction/ServiceRole/DefaultPolicy/Resource',
];
for (const path of pipelineWildcardPaths) {
  NagSuppressions.addResourceSuppressionsByPath(pipeline, path, [
    { id: 'AwsSolutions-IAM5', reason: 'S3 bucket 内 prefix (raw/*, normalized/*, build/*, latest/manifest.json, generations/*) と DynamoDB LeadingKeys 条件で絞られた wildcard。' },
  ]);
}
// API Lambda: dist bucket の latest/manifest.json 単一オブジェクトへの GetObject
// (実質 wildcard なしのはずだが CDK が GetObject* を付ける場合に備え保険)
NagSuppressions.addResourceSuppressionsByPath(
  api,
  '/MedicalAccessLod-dev-Api/ApiFunction/ServiceRole/DefaultPolicy/Resource',
  [
    { id: 'AwsSolutions-IAM5', reason: 'DynamoDB Query/GetItem と S3 latest/manifest.json への read のみ。CDK 付与の GetObject Version* は同一 key に閉じている。' },
  ],
);
// CleanupRescanRole は lambda:InvokeFunction on 単一関数のみで wildcard 無しの想定。
// CDK が `lambda:InvokeFunction` の *:Version 展開で wildcard を付けたら以下を有効化する:
NagSuppressions.addResourceSuppressionsByPath(
  pipeline,
  '/MedicalAccessLod-dev-Pipeline/CleanupRescanRole/DefaultPolicy/Resource',
  [
    { id: 'AwsSolutions-IAM5', reason: 'lambda:InvokeFunction を単一 Cleanup Lambda に対して発火。CDK 付与の Version alias wildcard は許容。' },
  ],
);
// Step Functions State Machine Role: 各 Lambda の `arn:...:function:foo:*` (Version)
// を Invoke するため CDK が自動で :* wildcard を付ける。呼び出し先は Pipeline 内の
// 特定 Lambda 群に限定される (LambdaInvoke Task から派生) ため許容。
NagSuppressions.addResourceSuppressionsByPath(
  pipeline,
  '/MedicalAccessLod-dev-Pipeline/PipelineStateMachine/Role/DefaultPolicy/Resource',
  [
    { id: 'AwsSolutions-IAM5', reason: 'Step Functions が Pipeline 内の各 Lambda を lambda:InvokeFunction する際に CDK が付ける :Version wildcard。呼出先は列挙済み Lambda に限定される。' },
  ],
);
// api-stack の Lambda service role paths は Api stack で解決させる
NagSuppressions.addResourceSuppressionsByPath(
  api,
  '/MedicalAccessLod-dev-Api/ApiFunction/ServiceRole/Resource',
  [
    { id: 'AwsSolutions-IAM4', reason: 'AWSLambdaBasicExecutionRole (CDK 自動付与) を許容。' },
  ],
);
// 公開 LOD dist bucket は CloudFront OAC 経由で配信するため、S3 側の TLS 強制は
// bucket policy で担保する設計 (直接 https-only bucket policy が nag で見えない)。
NagSuppressions.addStackSuppressions(delivery, [
  { id: 'AwsSolutions-S1', reason: 'dist バケットの read アクセスログは CloudFront 側 (logsBucket) で取得。dist bucket 自体の S3 サーバアクセスログはコスト削減のため無効。' },
  { id: 'AwsSolutions-S10', reason: 'CloudFront OAC 経由でのみアクセスされる。SSL は CloudFront 側で強制。' },
  { id: 'AwsSolutions-CFR4', reason: 'CloudFront default cert (TLSv1.2_2021) を使用。カスタム証明書は将来対応。' },
  // NOTE: AwsSolutions-CFR3 (CloudFront access logging) は delivery-stack.ts:51 で
  // enableLogging: true + logBucket 指定済みのため suppression 不要。
]);
NagSuppressions.addStackSuppressions(api, [
  { id: 'AwsSolutions-APIG1', reason: 'HTTP API のアクセスログは CloudWatch Metrics で代替 (コスト削減)。' },
  { id: 'AwsSolutions-APIG4', reason: '公開 LOD の read-only API のため認可は不要。' },
  { id: 'AwsSolutions-COG4', reason: '認可なしの公開 API (Cognito Authorizer は不要)。' },
]);

app.synth();
