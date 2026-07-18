import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as apigw from 'aws-cdk-lib/aws-apigatewayv2';
import * as integrations from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';

export interface ApiStackProps extends cdk.StackProps {
  envName: string;
  readModelTable: dynamodb.Table;
}

export class ApiStack extends cdk.Stack {
  public readonly apiFunction: lambda.Function;
  public readonly httpApi: apigw.HttpApi;

  constructor(scope: Construct, id: string, props: ApiStackProps) {
    super(scope, id, props);

    this.apiFunction = new lambda.Function(this, 'ApiFunction', {
      functionName: `medical-access-lod-${props.envName}-api`,
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'medical_access_lod.functions.api.handler.lambda_handler',
      code: lambda.Code.fromInline(
        'def lambda_handler(event, context):\n    return {"statusCode": 501, "body": "not deployed"}\n',
      ),
      memorySize: 512,
      timeout: cdk.Duration.seconds(10),
      tracing: lambda.Tracing.ACTIVE,
      logGroup: new logs.LogGroup(this, 'ApiFunctionLogs', {
        logGroupName: `/aws/lambda/medical-access-lod-${props.envName}-api`,
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
      environment: {
        ENVIRONMENT: props.envName,
        READ_MODEL_TABLE: props.readModelTable.tableName,
        POWERTOOLS_SERVICE_NAME: 'medical-access-lod-api',
        POWERTOOLS_METRICS_NAMESPACE: 'MedicalAccessLOD',
      },
    });

    props.readModelTable.grantReadData(this.apiFunction);

    this.httpApi = new apigw.HttpApi(this, 'HttpApi', {
      apiName: `medical-access-lod-${props.envName}`,
      corsPreflight: {
        allowOrigins: ['*'],
        allowMethods: [apigw.CorsHttpMethod.GET, apigw.CorsHttpMethod.OPTIONS],
        allowHeaders: ['content-type'],
        maxAge: cdk.Duration.hours(1),
      },
    });

    const integration = new integrations.HttpLambdaIntegration('ApiIntegration', this.apiFunction);

    for (const path of ['/health', '/facilities', '/facilities/{facility_id}', '/specialties', '/metadata']) {
      this.httpApi.addRoutes({
        path,
        methods: [apigw.HttpMethod.GET],
        integration,
      });
    }

    // Access logs
    const stage = this.httpApi.defaultStage!.node.defaultChild as apigw.CfnStage;
    const accessLogs = new logs.LogGroup(this, 'ApiAccessLogs', {
      logGroupName: `/aws/apigateway/medical-access-lod-${props.envName}`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    stage.accessLogSettings = {
      destinationArn: accessLogs.logGroupArn,
      format: JSON.stringify({
        requestId: '$context.requestId',
        ip: '$context.identity.sourceIp',
        requestTime: '$context.requestTime',
        httpMethod: '$context.httpMethod',
        routeKey: '$context.routeKey',
        status: '$context.status',
        protocol: '$context.protocol',
        responseLength: '$context.responseLength',
      }),
    };

    new cdk.CfnOutput(this, 'HttpApiUrl', { value: this.httpApi.apiEndpoint });
  }
}
