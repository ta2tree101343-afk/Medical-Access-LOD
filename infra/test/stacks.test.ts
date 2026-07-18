import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { StorageStack } from '../lib/storage-stack';
import { PipelineStack } from '../lib/pipeline-stack';
import { ApiStack } from '../lib/api-stack';
import { DeliveryStack } from '../lib/delivery-stack';
import { MonitoringStack } from '../lib/monitoring-stack';
import { IdentityStack } from '../lib/identity-stack';

function build(): {
  storage: StorageStack;
  pipeline: PipelineStack;
  api: ApiStack;
  delivery: DeliveryStack;
  monitoring: MonitoringStack;
  identity: IdentityStack;
} {
  const app = new cdk.App();
  const env = { account: '111111111111', region: 'ap-northeast-1' };
  const storage = new StorageStack(app, 'Storage', { env, envName: 'dev' });
  const delivery = new DeliveryStack(app, 'Delivery', { env, envName: 'dev' });
  const pipeline = new PipelineStack(app, 'Pipeline', {
    env,
    envName: 'dev',
    rawBucket: storage.rawBucket,
    normalizedBucket: storage.normalizedBucket,
    buildBucket: storage.buildBucket,
    distBucket: delivery.distBucket,
    readModelTable: storage.readModelTable,
    ecrRepository: storage.ecrRepository,
  });
  const api = new ApiStack(app, 'Api', {
    env,
    envName: 'dev',
    readModelTable: storage.readModelTable,
    ecrRepository: storage.ecrRepository,
  });
  const monitoring = new MonitoringStack(app, 'Monitoring', {
    env,
    envName: 'dev',
    pipelineStateMachine: pipeline.stateMachine,
    apiFunction: api.apiFunction,
    pipelineFunctions: pipeline.pipelineFunctions,
  });
  const identity = new IdentityStack(app, 'Identity', {
    env,
    envName: 'dev',
    githubOwner: 'test-owner',
    githubRepo: 'test-repo',
    ecrRepositoryArn: storage.ecrRepository.repositoryArn,
    distributionArn: `arn:aws:cloudfront::${env.account}:distribution/EXAMPLE`,
  });
  return { storage, pipeline, api, delivery, monitoring, identity };
}

describe('StorageStack', () => {
  const { storage } = build();
  const template = Template.fromStack(storage);

  test('all S3 buckets block public access', () => {
    template.allResourcesProperties('AWS::S3::Bucket', {
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
    });
  });

  test('all S3 buckets use SSE', () => {
    template.allResourcesProperties('AWS::S3::Bucket', {
      BucketEncryption: {
        ServerSideEncryptionConfiguration: Match.arrayWith([
          Match.objectLike({ ServerSideEncryptionByDefault: { SSEAlgorithm: 'AES256' } }),
        ]),
      },
    });
  });

  test('DynamoDB has GSIs for city+specialty and specialty+day', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({ IndexName: 'GSI1_CityBySpecialty' }),
        Match.objectLike({ IndexName: 'GSI2_SpecialtyByDay' }),
      ]),
    });
  });

  test('ECR scans images on push', () => {
    template.hasResourceProperties('AWS::ECR::Repository', {
      ImageScanningConfiguration: { ScanOnPush: true },
    });
  });
});

describe('PipelineStack', () => {
  const { pipeline } = build();
  const template = Template.fromStack(pipeline);

  test('has 6 Lambda functions (Docker image based)', () => {
    template.resourceCountIs('AWS::Lambda::Function', 6);
  });

  test('all Lambdas are arm64 with ACTIVE tracing', () => {
    template.allResourcesProperties('AWS::Lambda::Function', {
      Architectures: ['arm64'],
      TracingConfig: { Mode: 'Active' },
    });
  });

  test('state machine exists with tracing enabled', () => {
    template.hasResourceProperties('AWS::StepFunctions::StateMachine', {
      TracingConfiguration: { Enabled: true },
    });
  });

  test('bi-annual scheduler is configured for Asia/Tokyo', () => {
    template.hasResourceProperties('AWS::Scheduler::Schedule', {
      ScheduleExpression: 'cron(0 0 1 6,12 ? *)',
      ScheduleExpressionTimezone: 'Asia/Tokyo',
    });
  });
});

describe('ApiStack', () => {
  const { api } = build();
  const template = Template.fromStack(api);

  test('HTTP API is created', () => {
    template.hasResourceProperties('AWS::ApiGatewayV2::Api', {
      ProtocolType: 'HTTP',
    });
  });

  test('routes exist for /health, /facilities, /specialties, /metadata', () => {
    for (const route of ['GET /health', 'GET /facilities', 'GET /specialties', 'GET /metadata']) {
      template.hasResourceProperties('AWS::ApiGatewayV2::Route', {
        RouteKey: route,
      });
    }
  });
});

describe('DeliveryStack', () => {
  const { delivery } = build();
  const template = Template.fromStack(delivery);

  test('CloudFront distribution uses OAC and HTTPS-only', () => {
    template.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({
        DefaultCacheBehavior: Match.objectLike({
          ViewerProtocolPolicy: 'redirect-to-https',
        }),
      }),
    });
    template.resourceCountIs('AWS::CloudFront::OriginAccessControl', 1);
  });
});

describe('MonitoringStack', () => {
  const { monitoring } = build();
  const template = Template.fromStack(monitoring);

  test('SNS alert topic and CloudWatch alarms exist', () => {
    template.resourceCountIs('AWS::SNS::Topic', 1);
    // pipeline_failed + shacl + api_5xx + per-fn (Errors+Throttles) = 1+1+1+6*2 = 15
    template.resourceCountIs('AWS::CloudWatch::Alarm', 15);
  });
});

describe('IdentityStack', () => {
  const { identity } = build();
  const template = Template.fromStack(identity);

  test('OIDC provider is registered', () => {
    // CDK creates OIDC provider via a custom resource
    template.resourceCountIs('Custom::AWSCDKOpenIdConnectProvider', 1);
  });

  test('deploy role trusts GitHub OIDC with repo condition', () => {
    template.hasResourceProperties('AWS::IAM::Role', {
      AssumeRolePolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Principal: Match.objectLike({ Federated: Match.anyValue() }),
            Condition: Match.objectLike({
              StringLike: Match.objectLike({
                'token.actions.githubusercontent.com:sub': Match.stringLikeRegexp('repo:test-owner/test-repo:\\*'),
              }),
            }),
          }),
        ]),
      }),
    });
  });
});
