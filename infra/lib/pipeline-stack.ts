import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as tasks from 'aws-cdk-lib/aws-stepfunctions-tasks';
import * as scheduler from 'aws-cdk-lib/aws-scheduler';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as lambdaEventSources from 'aws-cdk-lib/aws-lambda-event-sources';
import { Construct } from 'constructs';

export interface PipelineStackProps extends cdk.StackProps {
  envName: string;
  snapshotDate: string;
  sourceUrl: string;
  rawBucket: s3.Bucket;
  normalizedBucket: s3.Bucket;
  buildBucket: s3.Bucket;
  distBucket: s3.Bucket;
  readModelTable: dynamodb.Table;
  ecrRepository: ecr.Repository;
}

const SNAPSHOT_DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

function validateSourceConfig(snapshotDate: string, sourceUrl: string): void {
  if (!SNAPSHOT_DATE_PATTERN.test(snapshotDate)) {
    throw new Error(`snapshotDate must use YYYY-MM-DD format: ${snapshotDate}`);
  }

  const [year, month, day] = snapshotDate.split('-').map(Number);
  const parsed = new Date(Date.UTC(year, month - 1, day));
  if (
    parsed.getUTCFullYear() !== year ||
    parsed.getUTCMonth() !== month - 1 ||
    parsed.getUTCDate() !== day
  ) {
    throw new Error(`snapshotDate is not a valid calendar date: ${snapshotDate}`);
  }

  let parsedUrl: URL;
  try {
    parsedUrl = new URL(sourceUrl);
  } catch {
    throw new Error(`sourceUrl must be a valid URL: ${sourceUrl}`);
  }
  if (parsedUrl.protocol !== 'https:') {
    throw new Error(`sourceUrl must use HTTPS: ${sourceUrl}`);
  }

  const compactDate = snapshotDate.replaceAll('-', '');
  if (!parsedUrl.href.includes(compactDate)) {
    throw new Error(
      `sourceUrl must contain snapshotDate as YYYYMMDD (${compactDate}): ${sourceUrl}`,
    );
  }
}

interface FnSpec {
  key: string;
  handler: string;
  timeoutSeconds?: number;
  memoryMB?: number;
}

const FUNCTIONS: FnSpec[] = [
  { key: 'Download', handler: 'medical_access_lod.functions.download.handler.lambda_handler' },
  { key: 'Normalize', handler: 'medical_access_lod.functions.normalize.handler.lambda_handler', memoryMB: 3008, timeoutSeconds: 600 },
  { key: 'BuildRdf', handler: 'medical_access_lod.functions.build_rdf.handler.lambda_handler', memoryMB: 3008, timeoutSeconds: 600 },
  { key: 'Validate', handler: 'medical_access_lod.functions.validate.handler.lambda_handler', memoryMB: 3008, timeoutSeconds: 900 },
  { key: 'Publish', handler: 'medical_access_lod.functions.publish.handler.lambda_handler' },
  { key: 'BuildReadModel', handler: 'medical_access_lod.functions.build_read_model.handler.lambda_handler', memoryMB: 2048, timeoutSeconds: 600 },
];

export class PipelineStack extends cdk.Stack {
  public readonly stateMachine: sfn.StateMachine;
  public readonly pipelineFunctions: Record<string, lambda.Function>;

  constructor(scope: Construct, id: string, props: PipelineStackProps) {
    super(scope, id, props);

    validateSourceConfig(props.snapshotDate, props.sourceUrl);

    const commonEnv = {
      ENVIRONMENT: props.envName,
      RAW_BUCKET: props.rawBucket.bucketName,
      NORMALIZED_BUCKET: props.normalizedBucket.bucketName,
      BUILD_BUCKET: props.buildBucket.bucketName,
      DIST_BUCKET: props.distBucket.bucketName,
      READ_MODEL_TABLE: props.readModelTable.tableName,
      POWERTOOLS_SERVICE_NAME: 'medical-access-lod',
      POWERTOOLS_METRICS_NAMESPACE: 'MedicalAccessLOD',
    };

    const fns: Record<string, lambda.Function> = {};
    for (const spec of FUNCTIONS) {
      const fn = new lambda.DockerImageFunction(this, `${spec.key}Function`, {
        functionName: `medical-access-lod-${props.envName}-${spec.key.toLowerCase()}`,
        code: lambda.DockerImageCode.fromEcr(props.ecrRepository, {
          tagOrDigest: spec.key.toLowerCase(),
          cmd: [spec.handler],
        }),
        architecture: lambda.Architecture.ARM_64,
        memorySize: spec.memoryMB ?? 1024,
        timeout: cdk.Duration.seconds(spec.timeoutSeconds ?? 300),
        ephemeralStorageSize: cdk.Size.mebibytes(2048),
        tracing: lambda.Tracing.ACTIVE,
        logGroup: new logs.LogGroup(this, `${spec.key}LogGroup`, {
          logGroupName: `/aws/lambda/medical-access-lod-${props.envName}-${spec.key.toLowerCase()}`,
          retention: logs.RetentionDays.ONE_MONTH,
          removalPolicy: cdk.RemovalPolicy.DESTROY,
        }),
        environment: { ...commonEnv, FUNCTION_KEY: spec.key },
      });
      fns[spec.key] = fn;
    }

    // Grant least-privilege bucket access per function
    props.rawBucket.grantReadWrite(fns.Download);
    props.rawBucket.grantRead(fns.Normalize);
    props.normalizedBucket.grantReadWrite(fns.Normalize);
    props.normalizedBucket.grantRead(fns.BuildRdf);
    props.normalizedBucket.grantRead(fns.BuildReadModel);
    props.buildBucket.grantReadWrite(fns.BuildRdf);
    props.buildBucket.grantReadWrite(fns.Validate);
    props.buildBucket.grantRead(fns.Publish);
    // BuildReadModel は世代 inventory (PK/SK 一覧) を build_bucket の
    // generations/ prefix 下に gzip 分割で書き出す。
    // Cleanup Lambda が BatchWriteItem で使う削除リストの唯一のソース。
    props.buildBucket.grantWrite(fns.BuildReadModel, 'generations/*');
    props.distBucket.grantReadWrite(fns.Publish);
    props.readModelTable.grantReadWriteData(fns.BuildReadModel);
    // Publish は lock の lease 更新・解放に加え、条件失敗時に現在所有者を
    // GetItem で確認して「完了済み再試行」と「別実行との競合」を区別する。
    // また manifest CAS 成功後に SYSTEM#GENERATION#RUN#<run_id> を COMMITTED
    // に遷移させる (Cleanup Lambda がこの状態のみを対象にする)。
    // generation 本体 (GENERATION#*) は書けない (旧世代の誤破壊防止)。
    fns.Publish.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:GetItem', 'dynamodb:UpdateItem', 'dynamodb:DeleteItem'],
      resources: [props.readModelTable.tableArn],
      conditions: {
        'ForAllValues:StringEquals': {
          'dynamodb:LeadingKeys': ['SYSTEM#PIPELINE', 'SYSTEM#GENERATION'],
        },
      },
    }));

    this.pipelineFunctions = fns;

    // ==== Cleanup Lambda (旧世代 GC) ====
    //
    // Publish の manifest CAS commit 後、SFN が SQS メッセージを送って発火する。
    // Publish とは別の IAM Role を持ち、SYSTEM#PIPELINE (lock) にはアクセス不可。
    // GENERATION#* (実データ) と SYSTEM#GENERATION (catalog) のみ操作する。
    //
    // 削除ポリシー: 現行 manifest 世代を絶対に消さない + 直近 N 世代 (default 6)
    //   + 最低保持期間 (default 365 日) を維持する。詳細は
    //   src/medical_access_lod/functions/shared/generation_retention.py。
    const cleanupDlq = new sqs.Queue(this, 'CleanupDLQ', {
      queueName: `medical-access-lod-${props.envName}-cleanup-dlq`,
      retentionPeriod: cdk.Duration.days(14),
      enforceSSL: true,
    });
    const cleanupQueue = new sqs.Queue(this, 'CleanupQueue', {
      queueName: `medical-access-lod-${props.envName}-cleanup`,
      // Cleanup は分単位で終わる想定。visibility は Lambda timeout の 6 倍。
      visibilityTimeout: cdk.Duration.minutes(30),
      retentionPeriod: cdk.Duration.days(4),
      enforceSSL: true,
      deadLetterQueue: {
        queue: cleanupDlq,
        maxReceiveCount: 3,
      },
    });

    const cleanupFn = new lambda.DockerImageFunction(this, 'CleanupFunction', {
      functionName: `medical-access-lod-${props.envName}-cleanup`,
      code: lambda.DockerImageCode.fromEcr(props.ecrRepository, {
        tagOrDigest: 'cleanup',
        cmd: ['medical_access_lod.functions.cleanup.handler.lambda_handler'],
      }),
      architecture: lambda.Architecture.ARM_64,
      memorySize: 1024,
      timeout: cdk.Duration.minutes(5),
      tracing: lambda.Tracing.ACTIVE,
      logGroup: new logs.LogGroup(this, 'CleanupLogGroup', {
        logGroupName: `/aws/lambda/medical-access-lod-${props.envName}-cleanup`,
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
      environment: {
        ENVIRONMENT: props.envName,
        POWERTOOLS_SERVICE_NAME: 'medical-access-lod-cleanup',
        POWERTOOLS_METRICS_NAMESPACE: 'MedicalAccessLOD',
        FUNCTION_KEY: 'Cleanup',
      },
    });

    // Cleanup IAM: SYSTEM#PIPELINE (Publish lock) には決して触らせない。
    //
    // 実 PK の形は 2 種類あり、条件式が異なる:
    //   - catalog: PK = "SYSTEM#GENERATION" (単一値の完全一致)
    //   - 実データ: PK = "GENERATION#<run_id>#FACILITY#<fid>" (プレフィックス一致)
    // StringEquals では後者にマッチしないため BatchWriteItem が AccessDenied に
    // なる。Statement を分けて catalog は StringEquals、データは StringLike で許可。
    //
    // Statement A: catalog エントリ (SYSTEM#GENERATION) — get/list/status 遷移
    cleanupFn.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'dynamodb:GetItem',
        'dynamodb:UpdateItem',
        'dynamodb:Query',
      ],
      resources: [props.readModelTable.tableArn],
      conditions: {
        'ForAllValues:StringEquals': {
          'dynamodb:LeadingKeys': ['SYSTEM#GENERATION'],
        },
      },
    }));
    // Statement B: 実データ (GENERATION#*) — BatchWriteItem による削除のみ
    // DeleteItem は現状未使用 (BatchWriteItem に集約) のため付与しない。
    cleanupFn.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'dynamodb:BatchWriteItem',
      ],
      resources: [props.readModelTable.tableArn],
      conditions: {
        'ForAllValues:StringLike': {
          'dynamodb:LeadingKeys': ['GENERATION#*'],
        },
      },
    }));
    // manifest 参照 (現行世代の判定に必須)
    props.distBucket.grantRead(cleanupFn, 'latest/manifest.json');
    // inventory 参照 (BatchWriteItem 対象キーの列挙)
    props.buildBucket.grantRead(cleanupFn, 'generations/*');

    // SQS → Cleanup Lambda
    cleanupFn.addEventSource(new lambdaEventSources.SqsEventSource(cleanupQueue, {
      batchSize: 1,  // 各メッセージは全世代の GC を試みるので並列不要
      reportBatchItemFailures: true,
    }));

    // Step Functions
    //
    // 各 Lambda の入力仕様 (Pydantic) は src/medical_access_lod/functions/shared/events.py
    // 参照。前段 Lambda の戻り値だけでは次段の必須項目 (normalized_bucket 等) が
    // 揃わないため、先頭で `InjectContext` により bucket/table 名を state に注入し、
    // 各 Task では `resultPath: '$.<stage>'` で戻り値を名前空間に格納して以降も
    // 参照できるようにする。
    //
    // Scheduler 入力に期待する形:
    //   { "snapshot_date": "YYYY-MM-DD", "source_url": "https://.../e-govYYYYMMDD.zip" }
    // 半年ごとの MHLW 公開に合わせ、運用者が Scheduler 入力を更新する運用。
    const injectContext = new sfn.Pass(this, 'InjectContext', {
      parameters: {
        // Execution.Name は最大 80 文字。events.BaseEvent.run_id は max_length=128 で受け入れる。
        run_id: sfn.JsonPath.stringAt('$$.Execution.Name'),
        snapshot_date: sfn.JsonPath.stringAt('$.snapshot_date'),
        source_url: sfn.JsonPath.stringAt('$.source_url'),
        raw_bucket: props.rawBucket.bucketName,
        normalized_bucket: props.normalizedBucket.bucketName,
        build_bucket: props.buildBucket.bucketName,
        dist_bucket: props.distBucket.bucketName,
        read_model_table: props.readModelTable.tableName,
      },
    });

    const download = new tasks.LambdaInvoke(this, 'DownloadTask', {
      lambdaFunction: fns.Download,
      payload: sfn.TaskInput.fromObject({
        run_id: sfn.JsonPath.stringAt('$.run_id'),
        source_url: sfn.JsonPath.stringAt('$.source_url'),
        snapshot_date: sfn.JsonPath.stringAt('$.snapshot_date'),
        raw_bucket: sfn.JsonPath.stringAt('$.raw_bucket'),
      }),
      payloadResponseOnly: true,
      resultPath: '$.download',
    });

    const normalize = new tasks.LambdaInvoke(this, 'NormalizeTask', {
      lambdaFunction: fns.Normalize,
      payload: sfn.TaskInput.fromObject({
        run_id: sfn.JsonPath.stringAt('$.run_id'),
        raw_bucket: sfn.JsonPath.stringAt('$.raw_bucket'),
        raw_prefix: sfn.JsonPath.stringAt('$.download.raw_prefix'),
        normalized_bucket: sfn.JsonPath.stringAt('$.normalized_bucket'),
      }),
      payloadResponseOnly: true,
      resultPath: '$.normalize',
    });

    const buildRdf = new tasks.LambdaInvoke(this, 'BuildRdfTask', {
      lambdaFunction: fns.BuildRdf,
      payload: sfn.TaskInput.fromObject({
        run_id: sfn.JsonPath.stringAt('$.run_id'),
        normalized_bucket: sfn.JsonPath.stringAt('$.normalized_bucket'),
        normalized_key: sfn.JsonPath.stringAt('$.normalize.normalized_key'),
        build_bucket: sfn.JsonPath.stringAt('$.build_bucket'),
      }),
      payloadResponseOnly: true,
      resultPath: '$.build_rdf',
    });

    const validate = new tasks.LambdaInvoke(this, 'ValidateTask', {
      lambdaFunction: fns.Validate,
      payload: sfn.TaskInput.fromObject({
        run_id: sfn.JsonPath.stringAt('$.run_id'),
        build_bucket: sfn.JsonPath.stringAt('$.build_bucket'),
        ttl_key: sfn.JsonPath.stringAt('$.build_rdf.ttl_key'),
      }),
      payloadResponseOnly: true,
      resultPath: '$.validate',
    });

    const publish = new tasks.LambdaInvoke(this, 'PublishTask', {
      lambdaFunction: fns.Publish,
      payload: sfn.TaskInput.fromObject({
        run_id: sfn.JsonPath.stringAt('$.run_id'),
        build_bucket: sfn.JsonPath.stringAt('$.build_bucket'),
        ttl_key: sfn.JsonPath.stringAt('$.build_rdf.ttl_key'),
        jsonld_key: sfn.JsonPath.stringAt('$.build_rdf.jsonld_key'),
        dist_bucket: sfn.JsonPath.stringAt('$.dist_bucket'),
        snapshot_date: sfn.JsonPath.stringAt('$.snapshot_date'),
        read_model_table: sfn.JsonPath.stringAt('$.read_model_table'),
        lock_owner: sfn.JsonPath.stringAt('$.read_model.lock_owner'),
        lock_expires_at: sfn.JsonPath.numberAt('$.read_model.lock_expires_at'),
      }),
      payloadResponseOnly: true,
      resultPath: '$.publish',
    });

    const readModel = new tasks.LambdaInvoke(this, 'BuildReadModelTask', {
      lambdaFunction: fns.BuildReadModel,
      payload: sfn.TaskInput.fromObject({
        run_id: sfn.JsonPath.stringAt('$.run_id'),
        normalized_bucket: sfn.JsonPath.stringAt('$.normalized_bucket'),
        normalized_key: sfn.JsonPath.stringAt('$.normalize.normalized_key'),
        read_model_table: sfn.JsonPath.stringAt('$.read_model_table'),
        // 世代 inventory (PK/SK 一覧) の書き出し先
        build_bucket: sfn.JsonPath.stringAt('$.build_bucket'),
        // generation catalog に STAGED で登録する際に必要
        snapshot_date: sfn.JsonPath.stringAt('$.snapshot_date'),
      }),
      payloadResponseOnly: true,
      resultPath: '$.read_model',
    });

    const notifyFailure = new sfn.Fail(this, 'NotifyFailure', {
      error: 'ShaclViolation',
      cause: 'RDF validation failed',
    });

    // Publish 完了後に Cleanup キューへ通知を送る。SFN は best-effort ではなく
    // 同期呼び出し (::sqs:sendMessage) にすることで、Publish 成功 = Cleanup
    // メッセージ enqueued 済み を保証する (実際の削除は非同期)。
    const notifyCleanup = new tasks.SqsSendMessage(this, 'NotifyCleanupQueue', {
      queue: cleanupQueue,
      messageBody: sfn.TaskInput.fromObject({
        trigger_run_id: sfn.JsonPath.stringAt('$.run_id'),
        read_model_table: sfn.JsonPath.stringAt('$.read_model_table'),
        inventory_bucket: sfn.JsonPath.stringAt('$.build_bucket'),
        dist_bucket: sfn.JsonPath.stringAt('$.dist_bucket'),
      }),
      resultPath: '$.cleanup_notice',
    });

    // 順序: ReadModel → Publish → NotifyCleanupQueue
    // ReadModel が失敗した場合に公開 S3 (dist bucket) を更新しないことで、
    // API が旧データ、公開ダンプが新データ という乖離を避ける。
    // (Publish 先行の場合は公開後に ReadModel 失敗すると乖離が発生する)
    const branchOnValidation = new sfn.Choice(this, 'IsRdfValid')
      .when(
        sfn.Condition.booleanEquals('$.validate.conforms', true),
        readModel.next(publish).next(notifyCleanup),
      )
      .otherwise(notifyFailure);

    const definition = injectContext
      .next(download)
      .next(normalize)
      .next(buildRdf)
      .next(validate)
      .next(branchOnValidation);

    this.stateMachine = new sfn.StateMachine(this, 'PipelineStateMachine', {
      stateMachineName: `medical-access-lod-${props.envName}-pipeline`,
      definitionBody: sfn.DefinitionBody.fromChainable(definition),
      timeout: cdk.Duration.hours(1),
      tracingEnabled: true,
      logs: {
        destination: new logs.LogGroup(this, 'PipelineLogs', {
          logGroupName: `/aws/vendedlogs/states/medical-access-lod-${props.envName}`,
          retention: logs.RetentionDays.ONE_MONTH,
          removalPolicy: cdk.RemovalPolicy.DESTROY,
        }),
        level: sfn.LogLevel.ALL,
        includeExecutionData: false,
      },
    });

    // EventBridge Scheduler: 半年ごと (6月1日 / 12月1日 00:00 JST)
    const schedulerRole = new iam.Role(this, 'SchedulerRole', {
      assumedBy: new iam.ServicePrincipal('scheduler.amazonaws.com'),
    });
    this.stateMachine.grantStartExecution(schedulerRole);

    // 半年ごとの MHLW 公開 (6/1, 12/1) に合わせた定期実行。
    //
    // `cron(0 0 1 6,12 ? *)` + Asia/Tokyo は **00:00 JST** (深夜) を意味する。
    // (AWS Scheduler の cron 式のフィールド順は minute/hour/day/month/day-of-week/year)
    // (docs: https://docs.aws.amazon.com/scheduler/latest/UserGuide/schedule-types.html)
    //
    // snapshot_date と source_url は CDK context (`snapshotDate`, `sourceUrl`) から
    // デプロイ時に更新する。constructor で HTTPS・日付形式・URL 内 YYYYMMDD の一致を
    // 検証するため、片方だけを更新した設定は synth 時に失敗する。
    //
    // BuildReadModel が期限付き DynamoDB lock を取得し、Publish が manifest commit 後に
    // 解放する。同時実行は lock 取得時に拒否され、異常終了時も期限後に回復できる。
    new scheduler.CfnSchedule(this, 'BiannualSchedule', {
      name: `medical-access-lod-${props.envName}-biannual`,
      scheduleExpression: 'cron(0 0 1 6,12 ? *)',
      scheduleExpressionTimezone: 'Asia/Tokyo',
      flexibleTimeWindow: { mode: 'OFF' },
      target: {
        arn: this.stateMachine.stateMachineArn,
        roleArn: schedulerRole.roleArn,
        input: JSON.stringify({
          snapshot_date: props.snapshotDate,
          source_url: props.sourceUrl,
        }),
      },
      state: 'ENABLED',
    });

    new cdk.CfnOutput(this, 'StateMachineArn', { value: this.stateMachine.stateMachineArn });
  }
}
