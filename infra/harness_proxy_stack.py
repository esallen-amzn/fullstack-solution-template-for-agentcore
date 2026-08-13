"""
CDK Stack: FAST Harness Proxy
==============================
Deploys:
  - Lambda function (Python 3.12) with the harness proxy handler
  - API Gateway HTTP API with CORS
  - Cognito User Pool + App Client + Hosted UI domain (created by this stack)
  - Amplify Hosting app + staging S3 bucket for the React frontend
  - IAM permissions for bedrock-agentcore:InvokeHarness

Usage:
  cd infra/ && source .env  # set HARNESS_ACCOUNT, HARNESS_REGION, HARNESS_ID, RUNTIME_ID
  cdk deploy
"""

import os

from aws_cdk import (
    App,
    Stack,
    Duration,
    CfnOutput,
    RemovalPolicy,
    aws_lambda as _lambda,
    aws_iam as iam,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as integrations,
    aws_apigatewayv2_authorizers as authorizers,
    aws_cognito as cognito,
    aws_logs as logs,
)
from constructs import Construct

from amplify_hosting_construct import AmplifyHostingConstruct


class HarnessProxyStack(Stack):
    """
    Stack that deploys the FAST-to-Harness proxy Lambda behind an HTTP API.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ----------------------------------------------------------------------
        # Configuration
        # ----------------------------------------------------------------------
        account_id = os.environ.get("HARNESS_ACCOUNT", "CHANGE_ME")
        region = os.environ.get("HARNESS_REGION", "us-east-1")
        harness_id = os.environ.get("HARNESS_ID", "CHANGE_ME")
        runtime_id = os.environ.get("RUNTIME_ID", "CHANGE_ME")
        agent_name = os.environ.get("AGENT_NAME", "my-harness-agent")
        cognito_domain_prefix = os.environ.get("COGNITO_DOMAIN_PREFIX", "fast-harness")

        harness_arn = (
            f"arn:aws:bedrock-agentcore:{region}:{account_id}:harness/{harness_id}"
        )
        runtime_arn = (
            f"arn:aws:bedrock-agentcore:{region}:{account_id}:runtime/{runtime_id}"
        )

        # ----------------------------------------------------------------------
        # Cognito User Pool (created and owned by this stack)
        # ----------------------------------------------------------------------
        user_pool = cognito.UserPool(
            self,
            "HarnessUserPool",
            user_pool_name="fast-harness-users",
            self_sign_up_enabled=False,  # Admin-only user creation
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            password_policy=cognito.PasswordPolicy(
                min_length=8,
                require_digits=True,
                require_lowercase=True,
                require_uppercase=True,
                require_symbols=False,
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ----------------------------------------------------------------------
        # Amplify Hosting (frontend)
        # Ported from infra-cdk/lib/amplify-hosting-construct.ts
        # ----------------------------------------------------------------------
        amplify_hosting = AmplifyHostingConstruct(
            self,
            "AmplifyHosting",
            app_name_prefix="fast-harness",
        )

        user_pool_client = user_pool.add_client(
            "HarnessAppClient",
            user_pool_client_name="fast-harness-frontend",
            auth_flows=cognito.AuthFlow(
                user_srp=True,
                user_password=True,
            ),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(
                    authorization_code_grant=True,
                    implicit_code_grant=True,
                ),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
                callback_urls=[
                    "http://localhost:3000",
                    amplify_hosting.amplify_url,
                ],
                logout_urls=[
                    "http://localhost:3000",
                    amplify_hosting.amplify_url,
                ],
            ),
        )

        # Cognito domain for Hosted UI
        user_pool.add_domain(
            "HarnessDomain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=cognito_domain_prefix
            ),
        )

        # ----------------------------------------------------------------------
        # Lambda Function
        # ----------------------------------------------------------------------
        proxy_function = _lambda.Function(
            self,
            "HarnessProxyFunction",
            function_name="fast-harness-proxy",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=_lambda.Code.from_asset("../lambda/harness_proxy"),
            timeout=Duration.seconds(120),  # Harness calls can take time
            memory_size=256,
            environment={
                "HARNESS_ARN": harness_arn,
                "RUNTIME_ARN": runtime_arn,
                "AGENT_NAME": agent_name,
                "AWS_REGION_OVERRIDE": "us-east-1",
            },
            log_retention=logs.RetentionDays.TWO_WEEKS,
        )

        # Grant permission to invoke the Harness
        proxy_function.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "bedrock-agentcore:InvokeHarness",
                    "bedrock-agentcore:InvokeAgentRuntime",
                ],
                resources=[
                    harness_arn,
                    runtime_arn,
                    # Wildcard for session-scoped resources
                    f"{harness_arn}/*",
                    f"{runtime_arn}/*",
                ],
            )
        )

        # ----------------------------------------------------------------------
        # API Gateway HTTP API
        # ----------------------------------------------------------------------
        http_api = apigwv2.HttpApi(
            self,
            "HarnessProxyApi",
            api_name="fast-harness-proxy-api",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=["*"],  # Restrict to your CloudFront domain in prod
                allow_methods=[
                    apigwv2.CorsHttpMethod.POST,
                    apigwv2.CorsHttpMethod.OPTIONS,
                ],
                allow_headers=[
                    "Content-Type",
                    "Authorization",
                    "X-Session-Id",
                ],
                expose_headers=["X-Session-Id"],
                max_age=Duration.hours(1),
            ),
        )

        # Cognito JWT Authorizer
        authorizer = authorizers.HttpJwtAuthorizer(
            "CognitoAuthorizer",
            jwt_issuer=f"https://cognito-idp.us-east-1.amazonaws.com/{user_pool.user_pool_id}",
            jwt_audience=[user_pool_client.user_pool_client_id]
)

        # Lambda integration
        lambda_integration = integrations.HttpLambdaIntegration(
            "HarnessProxyIntegration",
            handler=proxy_function,
        )

        # POST /invoke route with auth
        http_api.add_routes(
            path="/invoke",
            methods=[apigwv2.HttpMethod.POST],
            integration=lambda_integration,
            authorizer=authorizer,
        )

        # Health check (no auth)
        http_api.add_routes(
            path="/health",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration(
                "HealthIntegration",
                handler=proxy_function,
            ),
        )

        # ----------------------------------------------------------------------
        # Outputs
        # ----------------------------------------------------------------------
        CfnOutput(
            self, "ApiUrl",
            value=http_api.url or "",
            description="API Gateway URL for the harness proxy",
        )
        CfnOutput(
            self, "InvokeEndpoint",
            value=f"{http_api.url}invoke",
            description="Full invoke endpoint URL (use this in FAST frontend config)",
        )
        CfnOutput(
            self, "UserPoolId",
            value=user_pool.user_pool_id,
            description="Cognito User Pool ID",
        )
        CfnOutput(
            self, "UserPoolClientId",
            value=user_pool_client.user_pool_client_id,
            description="Cognito App Client ID (for frontend auth)",
        )
        CfnOutput(
            self, "LambdaFunctionArn",
            value=proxy_function.function_arn,
            description="Proxy Lambda ARN",
        )

        # Aliases matching the names scripts/deploy-frontend.py expects, so the
        # existing FAST deploy-frontend.py script can target this stack directly.
        CfnOutput(
            self, "CognitoUserPoolId",
            value=user_pool.user_pool_id,
            description="Cognito User Pool ID (deploy-frontend.py alias)",
        )
        CfnOutput(
            self, "CognitoClientId",
            value=user_pool_client.user_pool_client_id,
            description="Cognito App Client ID (deploy-frontend.py alias)",
        )
        CfnOutput(
            self, "RuntimeArn",
            value=http_api.api_endpoint,
            description=(
                "Repurposed as the harness proxy base URL — the frontend "
                "appends /invoke to this value"
            ),
        )
        CfnOutput(
            self, "AmplifyUrl",
            value=amplify_hosting.amplify_url,
            description="Amplify Hosting app URL for the frontend",
        )
        CfnOutput(
            self, "AmplifyAppId",
            value=amplify_hosting.amplify_app.app_id,
            description="Amplify App ID (deploy-frontend.py target)",
        )
        CfnOutput(
            self, "StagingBucketName",
            value=amplify_hosting.staging_bucket.bucket_name,
            description="S3 bucket deploy-frontend.py uploads build artifacts to",
        )


# --------------------------------------------------------------------------
# App entrypoint
# --------------------------------------------------------------------------
app = App()
HarnessProxyStack(
    app,
    "FastHarnessProxyStack",
    env={
        "account": os.environ.get("HARNESS_ACCOUNT", os.environ.get("CDK_DEFAULT_ACCOUNT")),
        "region": os.environ.get("HARNESS_REGION", os.environ.get("CDK_DEFAULT_REGION", "us-east-1")),
    },
)
app.synth()
