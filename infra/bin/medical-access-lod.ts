#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
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

new MonitoringStack(app, `${prefix}-Monitoring`, {
  env,
  envName,
  pipelineStateMachine: pipeline.stateMachine,
  apiFunction: api.apiFunction,
  pipelineFunctions: pipeline.pipelineFunctions,
  cleanupFunction: pipeline.cleanupFunction,
  cleanupDlq: pipeline.cleanupDlq,
});

new IdentityStack(app, `${prefix}-Identity`, {
  env,
  envName,
  githubOwner,
  githubRepo,
  ecrRepositoryArn: storage.ecrRepository.repositoryArn,
  distributionArn: delivery.distributionArn,
  distBucket: delivery.distBucket,
});

app.synth();
