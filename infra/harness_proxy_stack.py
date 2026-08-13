"""
CDK Stack: FAST Harness Proxy
==============================
Deploys:
  - Lambda function (Python 3.12) with the harness proxy handler
  - API Gateway HTTP API with CORS
  - Cognito User Pool authorizer (placeholder — wire to existing pool or create new)
  - IAM permissions for bedrock-agentcore:InvokeHarness

Usage:
  cd infra/ && source .env  # set HARNESS_ACCOUNT, HARNESS_ARN_SUFFIX, RUNTIME_ARN_SUFFIX
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
        # Cognito User Pool (for Illumina team access)
        # Replace with existing pool ARN if you already have one from FAST deploy
        # ----------------------------------------------------------------------
        user_pool = cognito.UserPool(
            self,
            "IlluminaHarnessUserPool",
            user_pool_name="fast-harness-illumina-users",
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

        user_pool_client = user_pool.add_client(
            "IlluminaHarnessAppClient",
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
            ),
        )

        # Cognito domain for Hosted UI
        user_pool.add_domain(
            "IlluminaHarnessDomain",
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
