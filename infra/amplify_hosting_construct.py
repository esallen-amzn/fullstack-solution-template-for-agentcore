"""
CDK Construct: Amplify Hosting
==============================
Direct Python port of infra-cdk/lib/amplify-hosting-construct.ts from the
original FAST template, adapted for the harness proxy stack.

Creates:
  - A staging S3 bucket (+ access-logs bucket) that Amplify pulls
    deployment zips from (used by scripts/deploy-frontend.py)
  - An Amplify Hosting app with a "main" branch

Usage:
  from amplify_hosting_construct import AmplifyHostingConstruct
  amplify_hosting = AmplifyHostingConstruct(self, "AmplifyHosting", app_name_prefix="fast-harness")
"""

from aws_cdk import (
    Duration,
    RemovalPolicy,
    aws_s3 as s3,
    aws_iam as iam,
)
from aws_cdk import aws_amplify_alpha as amplify
from constructs import Construct


class AmplifyHostingConstruct(Construct):
    """
    Provisions an Amplify Hosting app plus the S3 staging bucket that
    scripts/deploy-frontend.py uploads build artifacts to.
    """

    def __init__(self, scope: Construct, construct_id: str, app_name_prefix: str) -> None:
        super().__init__(scope, construct_id)

        # Access logs bucket for the staging bucket
        access_logs_bucket = s3.Bucket(
            self,
            "StagingBucketAccessLogs",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            public_read_access=False,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="DeleteOldAccessLogs",
                    enabled=True,
                    expiration=Duration.days(90),
                )
            ],
        )

        # Staging bucket that Amplify pulls deployment zips from
        self.staging_bucket = s3.Bucket(
            self,
            "StagingBucket",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            versioned=True,  # required by Amplify start-deployment
            public_read_access=False,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            server_access_logs_bucket=access_logs_bucket,
            server_access_logs_prefix="staging-bucket-access-logs/",
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="DeleteOldDeployments",
                    enabled=True,
                    expiration=Duration.days(30),
                )
            ],
        )

        # Allow the Amplify service to read deployment artifacts
        self.staging_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AmplifyAccess",
                effect=iam.Effect.ALLOW,
                principals=[iam.ServicePrincipal("amplify.amazonaws.com")],
                actions=["s3:GetObject", "s3:GetObjectVersion"],
                resources=[self.staging_bucket.arn_for_objects("*")],
            )
        )

        # Enforce TLS for all requests to the bucket
        self.staging_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyInsecureConnections",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["s3:*"],
                resources=[
                    self.staging_bucket.bucket_arn,
                    self.staging_bucket.arn_for_objects("*"),
                ],
                conditions={"Bool": {"aws:SecureTransport": "false"}},
            )
        )

        # Amplify app
        self.amplify_app = amplify.App(
            self,
            "AmplifyApp",
            app_name=f"{app_name_prefix}-frontend",
            description=f"{app_name_prefix} - React Frontend",
            platform=amplify.Platform.WEB,
        )

        self.amplify_app.add_branch(
            "main",
            stage="PRODUCTION",
            branch_name="main",
        )

        # Predictable domain format: https://main.{appId}.amplifyapp.com
        self.amplify_url = f"https://main.{self.amplify_app.app_id}.amplifyapp.com"
