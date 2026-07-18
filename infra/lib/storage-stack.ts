import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import { Construct } from 'constructs';

export interface StorageStackProps extends cdk.StackProps {
  envName: string;
}

export class StorageStack extends cdk.Stack {
  public readonly rawBucket: s3.Bucket;
  public readonly normalizedBucket: s3.Bucket;
  public readonly buildBucket: s3.Bucket;
  public readonly readModelTable: dynamodb.Table;
  public readonly ecrRepository: ecr.Repository;

  constructor(scope: Construct, id: string, props: StorageStackProps) {
    super(scope, id, props);

    const bucketDefaults: Omit<s3.BucketProps, 'bucketName'> = {
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      lifecycleRules: [
        {
          noncurrentVersionExpiration: cdk.Duration.days(90),
        },
      ],
    };

    this.rawBucket = new s3.Bucket(this, 'RawBucket', {
      ...bucketDefaults,
      bucketName: `medical-access-lod-${props.envName}-raw`,
      lifecycleRules: [
        ...bucketDefaults.lifecycleRules!,
        { expiration: cdk.Duration.days(365), prefix: '' },
      ],
    });

    this.normalizedBucket = new s3.Bucket(this, 'NormalizedBucket', {
      ...bucketDefaults,
      bucketName: `medical-access-lod-${props.envName}-normalized`,
    });

    this.buildBucket = new s3.Bucket(this, 'BuildBucket', {
      ...bucketDefaults,
      bucketName: `medical-access-lod-${props.envName}-build`,
    });

    this.readModelTable = new dynamodb.Table(this, 'ReadModelTable', {
      tableName: `medical-access-lod-${props.envName}-read-model`,
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      deletionProtection: true,
    });

    this.readModelTable.addGlobalSecondaryIndex({
      indexName: 'GSI1_CityBySpecialty',
      partitionKey: { name: 'GSI1PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI1SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    this.readModelTable.addGlobalSecondaryIndex({
      indexName: 'GSI2_SpecialtyByDay',
      partitionKey: { name: 'GSI2PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI2SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    this.ecrRepository = new ecr.Repository(this, 'PipelineImages', {
      repositoryName: `medical-access-lod-${props.envName}-pipeline`,
      imageScanOnPush: true,
      encryption: ecr.RepositoryEncryption.AES_256,
      lifecycleRules: [
        { maxImageCount: 20, description: 'keep last 20 images' },
      ],
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      emptyOnDelete: false,
    });

    new cdk.CfnOutput(this, 'RawBucketName', { value: this.rawBucket.bucketName });
    new cdk.CfnOutput(this, 'ReadModelTableName', { value: this.readModelTable.tableName });
    new cdk.CfnOutput(this, 'EcrRepositoryUri', { value: this.ecrRepository.repositoryUri });
  }
}
