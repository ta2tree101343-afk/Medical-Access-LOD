import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as tasks from 'aws-cdk-lib/aws-stepfunctions-tasks';
import * as scheduler from 'aws-cdk-lib/aws-scheduler';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';

export interface PipelineStackProps extends cdk.StackProps {
  envName: string;
  rawBucket: s3.Bucket;
  normalizedBucket: s3.Bucket;
  buildBucket: s3.Bucket;
  distBucket: s3.Bucket;
  readModelTable: dynamodb.Table;
  ecrRepository: ecr.Repository;
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
    props.distBucket.grantReadWrite(fns.Publish);
    props.readModelTable.grantReadWriteData(fns.BuildReadModel);

    this.pipelineFunctions = fns;

    // Step Functions
    const download = new tasks.LambdaInvoke(this, 'DownloadTask', {
      lambdaFunction: fns.Download,
      outputPath: '$.Payload',
    });
    const normalize = new tasks.LambdaInvoke(this, 'NormalizeTask', {
      lambdaFunction: fns.Normalize,
      outputPath: '$.Payload',
    });
    const buildRdf = new tasks.LambdaInvoke(this, 'BuildRdfTask', {
      lambdaFunction: fns.BuildRdf,
      outputPath: '$.Payload',
    });
    const validate = new tasks.LambdaInvoke(this, 'ValidateTask', {
      lambdaFunction: fns.Validate,
      outputPath: '$.Payload',
    });
    const publish = new tasks.LambdaInvoke(this, 'PublishTask', {
      lambdaFunction: fns.Publish,
      outputPath: '$.Payload',
    });
    const readModel = new tasks.LambdaInvoke(this, 'BuildReadModelTask', {
      lambdaFunction: fns.BuildReadModel,
      outputPath: '$.Payload',
    });

    const notifyFailure = new sfn.Fail(this, 'NotifyFailure', {
      error: 'ShaclViolation',
      cause: 'RDF validation failed',
    });

    const branchOnValidation = new sfn.Choice(this, 'IsRdfValid')
      .when(sfn.Condition.booleanEquals('$.conforms', true), publish.next(readModel))
      .otherwise(notifyFailure);

    const definition = download
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

    // EventBridge Scheduler: 半年ごと (6月1日 / 12月1日 09:00 JST = 00:00 UTC)
    const schedulerRole = new iam.Role(this, 'SchedulerRole', {
      assumedBy: new iam.ServicePrincipal('scheduler.amazonaws.com'),
    });
    this.stateMachine.grantStartExecution(schedulerRole);

    new scheduler.CfnSchedule(this, 'BiannualSchedule', {
      name: `medical-access-lod-${props.envName}-biannual`,
      scheduleExpression: 'cron(0 0 1 6,12 ? *)',
      scheduleExpressionTimezone: 'Asia/Tokyo',
      flexibleTimeWindow: { mode: 'OFF' },
      target: {
        arn: this.stateMachine.stateMachineArn,
        roleArn: schedulerRole.roleArn,
        input: JSON.stringify({ source: 'scheduler' }),
      },
      state: 'ENABLED',
    });

    new cdk.CfnOutput(this, 'StateMachineArn', { value: this.stateMachine.stateMachineArn });
  }
}
