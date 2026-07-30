import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';

export interface IdentityStackProps extends cdk.StackProps {
  envName: string;
  githubOwner: string;
  githubRepo: string;
  ecrRepositoryArn: string;
  distributionArn: string;
  /** deploy.yml が `aws s3 sync lod/` する先 (dist bucket) */
  distBucket: s3.IBucket;
}

export class IdentityStack extends cdk.Stack {
  public readonly deployRole: iam.Role;

  constructor(scope: Construct, id: string, props: IdentityStackProps) {
    super(scope, id, props);

    // NOTE: OIDC Provider は AWS アカウント全体で 1 つしか作れない。
    // このコードは new で作成するため、同一アカウントで dev/stg/prod の
    // IdentityStack を全部 deploy すると 2 つ目以降で衝突する。
    // 対処: 初回に dev で作成し、stg/prod では OpenIdConnectProvider.fromOpenIdConnectProviderArn(...)
    // で import する分岐を導入する必要がある。詳細は infra/README.md
    // 「stg / prod への横展開」セクション参照。現状は dev 想定。
    const provider = new iam.OpenIdConnectProvider(this, 'GithubOidcProvider', {
      url: 'https://token.actions.githubusercontent.com',
      clientIds: ['sts.amazonaws.com'],
    });

    const conditions: iam.Conditions = {
      StringEquals: {
        'token.actions.githubusercontent.com:aud': 'sts.amazonaws.com',
      },
      StringLike: {
        'token.actions.githubusercontent.com:sub': `repo:${props.githubOwner}/${props.githubRepo}:*`,
      },
    };

    this.deployRole = new iam.Role(this, 'GithubDeployRole', {
      roleName: `medical-access-lod-${props.envName}-github-deploy`,
      assumedBy: new iam.WebIdentityPrincipal(provider.openIdConnectProviderArn, conditions),
      description: 'Role assumed by GitHub Actions to deploy CDK / push to ECR / invalidate CloudFront',
      maxSessionDuration: cdk.Duration.hours(1),
    });

    // CDK bootstrap requires access to specific CDK-managed roles (via sts:AssumeRole).
    this.deployRole.addToPolicy(new iam.PolicyStatement({
      actions: ['sts:AssumeRole'],
      resources: [`arn:aws:iam::${this.account}:role/cdk-*`],
    }));

    // ECR: push pipeline images
    this.deployRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'ecr:GetAuthorizationToken',
      ],
      resources: ['*'],
    }));
    this.deployRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'ecr:BatchCheckLayerAvailability',
        'ecr:CompleteLayerUpload',
        'ecr:GetDownloadUrlForLayer',
        'ecr:InitiateLayerUpload',
        'ecr:PutImage',
        'ecr:UploadLayerPart',
        'ecr:DescribeImages',
        'ecr:DescribeRepositories',
        'ecr:ListImages',
        'ecr:BatchGetImage',
      ],
      resources: [props.ecrRepositoryArn],
    }));

    // CloudFront invalidation (post-deploy cache flush)
    this.deployRole.addToPolicy(new iam.PolicyStatement({
      actions: ['cloudfront:CreateInvalidation'],
      resources: [props.distributionArn],
    }));

    // deploy.yml の `aws s3 sync lod/ s3://.../latest/` に必要な権限。
    // 書き込み系 (PutObject / DeleteObject / GetObject) は `latest/*` に限定し、
    // `releases/` や `archives/` は改変させない。ListBucket も `s3:prefix`
    // condition で `latest*` のみ許容し、他 prefix の一覧・存在確認をブロックする。
    //
    // NOTE: `s3:GetBucketLocation` はリクエスト時に `s3:prefix` context key を
    // 持たないため、prefix 条件付き Statement に混ぜると常に暗黙 Deny になる。
    // 別 Statement (無条件) に切り出す。`aws s3 sync` が region 解決で呼ぶ。
    this.deployRole.addToPolicy(new iam.PolicyStatement({
      actions: ['s3:ListBucket'],
      resources: [props.distBucket.bucketArn],
      conditions: {
        StringLike: {
          's3:prefix': ['latest/*', 'latest'],
        },
      },
    }));
    this.deployRole.addToPolicy(new iam.PolicyStatement({
      actions: ['s3:GetBucketLocation'],
      resources: [props.distBucket.bucketArn],
    }));
    this.deployRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        's3:PutObject',
        's3:DeleteObject',
        's3:GetObject',
      ],
      resources: [`${props.distBucket.bucketArn}/latest/*`],
    }));

    new cdk.CfnOutput(this, 'DeployRoleArn', { value: this.deployRole.roleArn });
    new cdk.CfnOutput(this, 'OidcProviderArn', { value: provider.openIdConnectProviderArn });
  }
}
