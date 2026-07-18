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

const prefix = `MedicalAccessLod-${envName}`;

const storage = new StorageStack(app, `${prefix}-Storage`, { env, envName });

const delivery = new DeliveryStack(app, `${prefix}-Delivery`, {
  env,
  envName,
});

const pipeline = new PipelineStack(app, `${prefix}-Pipeline`, {
  env,
  envName,
  rawBucket: storage.rawBucket,
  normalizedBucket: storage.normalizedBucket,
  buildBucket: storage.buildBucket,
  distBucket: delivery.distBucket,
  readModelTable: storage.readModelTable,
  ecrRepository: storage.ecrRepository,
});

const api = new ApiStack(app, `${prefix}-Api`, {
  env,
  envName,
  readModelTable: storage.readModelTable,
  ecrRepository: storage.ecrRepository,
});

new MonitoringStack(app, `${prefix}-Monitoring`, {
  env,
  envName,
  pipelineStateMachine: pipeline.stateMachine,
  apiFunction: api.apiFunction,
  pipelineFunctions: pipeline.pipelineFunctions,
});

new IdentityStack(app, `${prefix}-Identity`, {
  env,
  envName,
  githubOwner,
  githubRepo,
  ecrRepositoryArn: storage.ecrRepository.repositoryArn,
  distributionArn: delivery.distributionArn,
});

app.synth();
