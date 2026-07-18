import * as cdk from 'aws-cdk-lib';
import * as cw from 'aws-cdk-lib/aws-cloudwatch';
import * as actions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import { Construct } from 'constructs';

export interface MonitoringStackProps extends cdk.StackProps {
  envName: string;
  pipelineStateMachine: sfn.StateMachine;
  apiFunction: lambda.Function;
  pipelineFunctions: Record<string, lambda.Function>;
}

export class MonitoringStack extends cdk.Stack {
  public readonly alertTopic: sns.Topic;

  constructor(scope: Construct, id: string, props: MonitoringStackProps) {
    super(scope, id, props);

    this.alertTopic = new sns.Topic(this, 'AlertTopic', {
      topicName: `medical-access-lod-${props.envName}-alerts`,
      displayName: 'Medical Access LOD alerts',
    });
    const alertAction = new actions.SnsAction(this.alertTopic);

    // Step Functions execution failures
    new cw.Alarm(this, 'PipelineFailedAlarm', {
      alarmName: `medical-access-lod-${props.envName}-pipeline-failed`,
      metric: props.pipelineStateMachine.metricFailed({
        period: cdk.Duration.minutes(5),
        statistic: 'Sum',
      }),
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cw.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(alertAction);

    // Per-function alarms: Errors and Throttles
    for (const [name, fn] of Object.entries(props.pipelineFunctions)) {
      new cw.Alarm(this, `${name}ErrorAlarm`, {
        alarmName: `medical-access-lod-${props.envName}-${name.toLowerCase()}-errors`,
        metric: fn.metricErrors({ period: cdk.Duration.minutes(5), statistic: 'Sum' }),
        threshold: 1,
        evaluationPeriods: 1,
        comparisonOperator: cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        treatMissingData: cw.TreatMissingData.NOT_BREACHING,
      }).addAlarmAction(alertAction);

      new cw.Alarm(this, `${name}ThrottleAlarm`, {
        alarmName: `medical-access-lod-${props.envName}-${name.toLowerCase()}-throttles`,
        metric: fn.metricThrottles({ period: cdk.Duration.minutes(5), statistic: 'Sum' }),
        threshold: 1,
        evaluationPeriods: 1,
        comparisonOperator: cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        treatMissingData: cw.TreatMissingData.NOT_BREACHING,
      }).addAlarmAction(alertAction);
    }

    // API 5xx
    new cw.Alarm(this, 'Api5xxAlarm', {
      alarmName: `medical-access-lod-${props.envName}-api-5xx`,
      metric: props.apiFunction.metricErrors({ period: cdk.Duration.minutes(5), statistic: 'Sum' }),
      threshold: 5,
      evaluationPeriods: 1,
      comparisonOperator: cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cw.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(alertAction);

    // Custom SHACL violations metric (emitted by Validate Lambda via Powertools)
    const shaclViolations = new cw.Metric({
      namespace: 'MedicalAccessLOD',
      metricName: 'ShaclViolations',
      statistic: 'Sum',
      period: cdk.Duration.minutes(5),
      dimensionsMap: { service: 'medical-access-lod' },
    });
    new cw.Alarm(this, 'ShaclViolationsAlarm', {
      alarmName: `medical-access-lod-${props.envName}-shacl-violations`,
      metric: shaclViolations,
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cw.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(alertAction);

    // Dashboard
    new cw.Dashboard(this, 'PipelineDashboard', {
      dashboardName: `medical-access-lod-${props.envName}`,
      widgets: [
        [
          new cw.GraphWidget({
            title: 'Pipeline outcomes',
            left: [
              props.pipelineStateMachine.metricSucceeded(),
              props.pipelineStateMachine.metricFailed(),
              props.pipelineStateMachine.metricAborted(),
            ],
            width: 12,
          }),
          new cw.GraphWidget({
            title: 'SHACL violations',
            left: [shaclViolations],
            width: 12,
          }),
        ],
      ],
    });

    new cdk.CfnOutput(this, 'AlertTopicArn', { value: this.alertTopic.topicArn });
  }
}
