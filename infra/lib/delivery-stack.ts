import * as cdk from 'aws-cdk-lib';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';

export interface DeliveryStackProps extends cdk.StackProps {
  envName: string;
}

export class DeliveryStack extends cdk.Stack {
  public readonly distBucket: s3.Bucket;
  public readonly distribution: cloudfront.Distribution;
  public readonly distributionArn: string;

  constructor(scope: Construct, id: string, props: DeliveryStackProps) {
    super(scope, id, props);

    this.distBucket = new s3.Bucket(this, 'DistBucket', {
      bucketName: `medical-access-lod-${props.envName}-dist`,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      lifecycleRules: [{ noncurrentVersionExpiration: cdk.Duration.days(90) }],
    });

    const logsBucket = new s3.Bucket(this, 'CloudFrontLogs', {
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: false,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_PREFERRED,
      lifecycleRules: [{ expiration: cdk.Duration.days(90) }],
    });

    this.distribution = new cloudfront.Distribution(this, 'Distribution', {
      comment: `Medical Access LOD (${props.envName})`,
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(this.distBucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD,
        responseHeadersPolicy: cloudfront.ResponseHeadersPolicy.SECURITY_HEADERS,
        compress: true,
      },
      priceClass: cloudfront.PriceClass.PRICE_CLASS_200,
      httpVersion: cloudfront.HttpVersion.HTTP2_AND_3,
      enableLogging: true,
      logBucket: logsBucket,
      logFilePrefix: 'cloudfront-access-logs/',
      defaultRootObject: 'index.html',
      minimumProtocolVersion: cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
    });

    this.distributionArn = `arn:aws:cloudfront::${cdk.Stack.of(this).account}:distribution/${this.distribution.distributionId}`;

    new cdk.CfnOutput(this, 'DistBucketName', { value: this.distBucket.bucketName });
    new cdk.CfnOutput(this, 'DistributionDomain', { value: this.distribution.distributionDomainName });
    new cdk.CfnOutput(this, 'DistributionId', { value: this.distribution.distributionId });
  }
}
