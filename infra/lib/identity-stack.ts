import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

export interface IdentityStackProps extends cdk.StackProps {
  envName: string;
  githubOwner: string;
  githubRepo: string;
  ecrRepositoryArn: string;
  distributionArn: string;
}

export class IdentityStack extends cdk.Stack {
  public readonly deployRole: iam.Role;

  constructor(scope: Construct, id: string, props: IdentityStackProps) {
    super(scope, id, props);

    // Reuse if provider already exists in account; here we import.
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

    new cdk.CfnOutput(this, 'DeployRoleArn', { value: this.deployRole.roleArn });
    new cdk.CfnOutput(this, 'OidcProviderArn', { value: provider.openIdConnectProviderArn });
  }
}
