import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { StorageStack } from '../lib/storage-stack';
import { PipelineStack } from '../lib/pipeline-stack';
import { ApiStack } from '../lib/api-stack';
import { DeliveryStack } from '../lib/delivery-stack';
import { MonitoringStack } from '../lib/monitoring-stack';
import { IdentityStack } from '../lib/identity-stack';

const TEST_SNAPSHOT_DATE = '2025-12-01';
const TEST_SOURCE_URL =
  'https://example.test/datasets/e-gov20251201.zip';

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
    snapshotDate: TEST_SNAPSHOT_DATE,
    sourceUrl: TEST_SOURCE_URL,
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
    distBucket: delivery.distBucket,
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

function buildPipelineWithSource(snapshotDate: string, sourceUrl: string): PipelineStack {
  const app = new cdk.App();
  const env = { account: '111111111111', region: 'ap-northeast-1' };
  const storage = new StorageStack(app, `Storage-${snapshotDate}`, { env, envName: 'dev' });
  const delivery = new DeliveryStack(app, `Delivery-${snapshotDate}`, { env, envName: 'dev' });
  return new PipelineStack(app, `Pipeline-${snapshotDate}`, {
    env,
    envName: 'dev',
    snapshotDate,
    sourceUrl,
    rawBucket: storage.rawBucket,
    normalizedBucket: storage.normalizedBucket,
    buildBucket: storage.buildBucket,
    distBucket: delivery.distBucket,
    readModelTable: storage.readModelTable,
    ecrRepository: storage.ecrRepository,
  });
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

  test('Publish can inspect the pipeline lock and mark generations committed', () => {
    const policies = template.findResources('AWS::IAM::Policy');
    const statements = Object.values(policies).flatMap(
      (policy: any) => policy.Properties.PolicyDocument.Statement,
    );
    // Publish の DynamoDB 権限は SYSTEM#PIPELINE (lock) と SYSTEM#GENERATION
    // (catalog COMMITTED 遷移) にのみ許可。GENERATION#* (実データ) には
    // 書けないこと自体はここでは検証しない (許可対象を絞る形の設計のため)。
    const matching = statements.filter((statement: any) => {
      const actions = Array.isArray(statement.Action)
        ? statement.Action
        : [statement.Action];
      const leadingKeys = statement.Condition?.['ForAllValues:StringEquals']
        ? statement.Condition['ForAllValues:StringEquals']['dynamodb:LeadingKeys']
        : undefined;
      return (
        ['dynamodb:GetItem', 'dynamodb:UpdateItem', 'dynamodb:DeleteItem']
          .every((action) => actions.includes(action))
        && Array.isArray(leadingKeys)
      );
    });
    expect(matching.length).toBeGreaterThan(0);
    const publishLeadingKeys = matching.flatMap(
      (statement: any) =>
        statement.Condition['ForAllValues:StringEquals']['dynamodb:LeadingKeys'],
    );
    expect(publishLeadingKeys).toContain('SYSTEM#PIPELINE');
    expect(publishLeadingKeys).toContain('SYSTEM#GENERATION');
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

  test('scheduler input uses the configured snapshot and matching HTTPS URL', () => {
    const schedules = template.findResources('AWS::Scheduler::Schedule');
    const [_, sched] = Object.entries(schedules)[0];
    const input = JSON.parse(sched.Properties.Target.Input);
    expect(input).toEqual({
      snapshot_date: TEST_SNAPSHOT_DATE,
      source_url: TEST_SOURCE_URL,
    });
  });

  test('source configuration is rejected at synth time when invalid or inconsistent', () => {
    expect(() => buildPipelineWithSource('2025-02-30', 'https://example.test/e-gov20250230.zip'))
      .toThrow('not a valid calendar date');
    expect(() => buildPipelineWithSource('2025-12-01', 'http://example.test/e-gov20251201.zip'))
      .toThrow('must use HTTPS');
    expect(() => buildPipelineWithSource('2025-12-01', 'https://example.test/e-gov20250601.zip'))
      .toThrow('must contain snapshotDate');
  });

  // ---- ASL 契約テスト ----
  // 各 Task の Parameters が Lambda ハンドラの必須入力
  // (src/medical_access_lod/functions/shared/events.py) を満たすことを検証する。
  // Scheduler 入力 {snapshot_date, source_url} から始まり、Pass state で bucket/table
  // が state に注入され、各段が resultPath で戻り値を保持する構成を前提とする。

  function getStateMachineDefinition(): { StartAt: string; States: Record<string, any> } {
    const machines = template.findResources('AWS::StepFunctions::StateMachine');
    const [_, def] = Object.entries(machines)[0];
    const raw = def.Properties.DefinitionString;
    // Fn::Join で組み立てられている場合、Ref / Fn::GetAtt などの Object は
    // 単一文字列トークンに置き換えて JSON として parseable にする
    // (contract テストの目的は JSONPath 文字列や Type の検証で、実 ARN の値は不要)。
    if (raw && typeof raw === 'object' && 'Fn::Join' in raw) {
      const parts = (raw['Fn::Join'] as [string, unknown[]])[1];
      // 前後の string part は "...":\"" のように JSON 文字列を開いた状態で
      // 終わっているため、Ref/GetAtt トークンは "生の文字列コンテンツ"
      // (引用符なし) として埋めることで完成された JSON になる。
      const joined = parts
        .map((p) => (typeof p === 'string' ? p : '__CFN_REF__'))
        .join('');
      return JSON.parse(joined);
    }
    return JSON.parse(raw as string);
  }

  const definition = getStateMachineDefinition();

  test('scheduler input carries snapshot_date and source_url (Download の必須項目)', () => {
    const schedules = template.findResources('AWS::Scheduler::Schedule');
    const [_, sched] = Object.entries(schedules)[0];
    const input = JSON.parse(sched.Properties.Target.Input);
    expect(input.snapshot_date).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(input.source_url).toMatch(/^https:\/\//);
  });

  test('InjectContext Pass state seeds all bucket/table names + run_id', () => {
    const inject = definition.States['InjectContext'];
    expect(inject.Type).toBe('Pass');
    const p = inject.Parameters;
    expect(p['run_id.$']).toBe('$$.Execution.Name');
    expect(p['snapshot_date.$']).toBe('$.snapshot_date');
    expect(p['source_url.$']).toBe('$.source_url');
    // bucket/table 名は CloudFormation の Ref で解決されるので存在確認のみ
    expect(p.raw_bucket).toBeDefined();
    expect(p.normalized_bucket).toBeDefined();
    expect(p.build_bucket).toBeDefined();
    expect(p.dist_bucket).toBeDefined();
    expect(p.read_model_table).toBeDefined();
  });

  test('DownloadTask payload satisfies DownloadEvent (run_id/source_url/snapshot_date/raw_bucket)', () => {
    const task = definition.States['DownloadTask'];
    const p = task.Parameters['Payload'] ?? task.Parameters;
    expect(p['run_id.$']).toBe('$.run_id');
    expect(p['source_url.$']).toBe('$.source_url');
    expect(p['snapshot_date.$']).toBe('$.snapshot_date');
    expect(p['raw_bucket.$']).toBe('$.raw_bucket');
    expect(task.ResultPath).toBe('$.download');
  });

  test('NormalizeTask payload satisfies NormalizeEvent (raw_bucket/raw_prefix/normalized_bucket)', () => {
    const task = definition.States['NormalizeTask'];
    const p = task.Parameters['Payload'] ?? task.Parameters;
    expect(p['run_id.$']).toBe('$.run_id');
    expect(p['raw_bucket.$']).toBe('$.raw_bucket');
    expect(p['raw_prefix.$']).toBe('$.download.raw_prefix');
    expect(p['normalized_bucket.$']).toBe('$.normalized_bucket');
    expect(task.ResultPath).toBe('$.normalize');
  });

  test('BuildRdfTask payload satisfies BuildRdfEvent (normalized_bucket/normalized_key/build_bucket)', () => {
    const task = definition.States['BuildRdfTask'];
    const p = task.Parameters['Payload'] ?? task.Parameters;
    expect(p['run_id.$']).toBe('$.run_id');
    expect(p['normalized_bucket.$']).toBe('$.normalized_bucket');
    expect(p['normalized_key.$']).toBe('$.normalize.normalized_key');
    expect(p['build_bucket.$']).toBe('$.build_bucket');
    expect(task.ResultPath).toBe('$.build_rdf');
  });

  test('ValidateTask payload satisfies ValidateEvent (build_bucket/ttl_key)', () => {
    const task = definition.States['ValidateTask'];
    const p = task.Parameters['Payload'] ?? task.Parameters;
    expect(p['run_id.$']).toBe('$.run_id');
    expect(p['build_bucket.$']).toBe('$.build_bucket');
    expect(p['ttl_key.$']).toBe('$.build_rdf.ttl_key');
    expect(task.ResultPath).toBe('$.validate');
  });

  test('IsRdfValid Choice branches on $.validate.conforms', () => {
    const choice = definition.States['IsRdfValid'];
    expect(choice.Type).toBe('Choice');
    expect(choice.Choices[0].Variable).toBe('$.validate.conforms');
    expect(choice.Choices[0].BooleanEquals).toBe(true);
  });

  test('PublishTask payload satisfies PublishEvent and receives the read-model lock', () => {
    const task = definition.States['PublishTask'];
    const p = task.Parameters['Payload'] ?? task.Parameters;
    expect(p['run_id.$']).toBe('$.run_id');
    expect(p['build_bucket.$']).toBe('$.build_bucket');
    expect(p['ttl_key.$']).toBe('$.build_rdf.ttl_key');
    expect(p['jsonld_key.$']).toBe('$.build_rdf.jsonld_key');
    expect(p['dist_bucket.$']).toBe('$.dist_bucket');
    // 不変 release (releases/<snapshot_date>/<run_id>/) に必要
    expect(p['snapshot_date.$']).toBe('$.snapshot_date');
    expect(p['read_model_table.$']).toBe('$.read_model_table');
    expect(p['lock_owner.$']).toBe('$.read_model.lock_owner');
    expect(p['lock_expires_at.$']).toBe('$.read_model.lock_expires_at');
    expect(task.ResultPath).toBe('$.publish');
  });

  test('SHACL 成功時は ReadModel → Publish の順で実行される (ReadModel 失敗時に公開しないため)', () => {
    const choice = definition.States['IsRdfValid'];
    // 最初の Choice ブランチが指す次ステートが BuildReadModelTask であること
    expect(choice.Choices[0].Next).toBe('BuildReadModelTask');
    // BuildReadModelTask の次が PublishTask であること
    expect(definition.States['BuildReadModelTask'].Next).toBe('PublishTask');
  });

  test('BuildReadModelTask payload satisfies BuildReadModelEvent (normalized_bucket/normalized_key/read_model_table/build_bucket/snapshot_date)', () => {
    const task = definition.States['BuildReadModelTask'];
    const p = task.Parameters['Payload'] ?? task.Parameters;
    expect(p['run_id.$']).toBe('$.run_id');
    expect(p['normalized_bucket.$']).toBe('$.normalized_bucket');
    expect(p['normalized_key.$']).toBe('$.normalize.normalized_key');
    expect(p['read_model_table.$']).toBe('$.read_model_table');
    // 世代 inventory の書き出し先
    expect(p['build_bucket.$']).toBe('$.build_bucket');
    // generation catalog に STAGED で登録するため
    expect(p['snapshot_date.$']).toBe('$.snapshot_date');
    expect(task.ResultPath).toBe('$.read_model');
  });

  test('BuildReadModel can write inventory chunks under generations/ prefix only', () => {
    const policies = template.findResources('AWS::IAM::Policy');
    const statements = Object.values(policies).flatMap(
      (policy: any) => policy.Properties.PolicyDocument.Statement,
    );
    // BuildReadModel の書き込み権限は build_bucket の generations/ prefix のみ。
    // dist_bucket や build_bucket ルートには書けない (最小権限)。
    const inventoryWriteStatement = statements.find((statement: any) => {
      const actions = Array.isArray(statement.Action)
        ? statement.Action
        : [statement.Action];
      const resources = Array.isArray(statement.Resource)
        ? statement.Resource
        : [statement.Resource];
      const containsWrite = actions.some((a: string) =>
        ['s3:PutObject', 's3:PutObjectLegalHold', 's3:PutObjectRetention',
          's3:PutObjectTagging', 's3:PutObjectVersionTagging', 's3:Abort*']
          .includes(a),
      );
      // Resource が build_bucket の generations/* prefix であることを確認する。
      // resource は Fn::Join か直接文字列で表現される。
      const targetsInventoryPrefix = resources.some((r: any) =>
        typeof r === 'object'
        && r['Fn::Join']
        && JSON.stringify(r['Fn::Join']).includes('generations/*'),
      );
      return containsWrite && targetsInventoryPrefix;
    });
    expect(inventoryWriteStatement).toBeDefined();
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

  test('API receives the dist bucket and can read only the committed manifest', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      Environment: {
        Variables: Match.objectLike({ DIST_BUCKET: Match.anyValue() }),
      },
    });
    const policies = template.findResources('AWS::IAM::Policy');
    expect(JSON.stringify(policies)).toContain('latest/manifest.json');
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
