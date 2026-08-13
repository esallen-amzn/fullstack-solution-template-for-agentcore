# FAST Harness Proxy — AgentCore Harness Web UI

Connect a React chat frontend to any [Amazon Bedrock AgentCore Harness](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness.html) agent via a Lambda proxy.

Based on the [Fullstack AgentCore Solution Template (FAST)](https://github.com/awslabs/fullstack-solution-template-for-agentcore) — adapted for the **Harness proxy pattern** where the agent already exists in AgentCore Harness and doesn't need to be deployed as a container.

> **This README describes the actual deployment path for this fork.** The
> `docs/` directory and `infra-cdk/` are carried over from the upstream FAST
> template and describe a *different* deployment (container-based AgentCore
> Runtime + full CDK stack) that this repo does not use. They're kept for
> reference only — follow this README, not `docs/DEPLOYMENT.md`, when
> deploying this harness proxy pattern. The actual infrastructure code lives
> in `infra/harness_proxy_stack.py`.

## Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────────────────┐
│  React Frontend │────▶│  API Gateway     │────▶│  Lambda Proxy               │
│  (localhost or  │     │  + Cognito JWT   │     │  (InvokeHarness → SSE)      │
│   CloudFront)   │     │  authorizer      │     │                             │
└─────────────────┘     └──────────────────┘     └──────────────┬──────────────┘
                                                                 │
                                                                 ▼
                                                  ┌─────────────────────────────┐
                                                  │  AgentCore Harness          │
                                                  │  (your existing agent)      │
                                                  └─────────────────────────────┘
```

**Why a proxy?** The FAST frontend expects SSE `data:` lines (InvokeAgentRuntime format), but Harness emits typed events (`messageStart`, `contentBlockDelta`, `messageStop`). The Lambda translates between formats.

---

## Prerequisites

- **AWS CLI v2** configured with credentials
- **Node.js 18+** and npm
- **Python 3.12+**
- **AWS CDK v2**: `npm install -g aws-cdk`
- **An existing AgentCore Harness agent** (deployed and in READY status)

---

## Quick Start

### 1. Clone and configure

```bash
git clone <this-repo-url>
cd fast-harness

# Set up infrastructure env vars
cp infra/.env.example infra/.env
# Edit infra/.env with your account, harness ID, etc.
```

### 2. Deploy the stack

```bash
cd infra
source .env
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cdk bootstrap aws://$HARNESS_ACCOUNT/$HARNESS_REGION  # first time only
cdk deploy
```

Note the outputs:
```
FastHarnessProxyStack.ApiUrl = https://XXXXXXXXXX.execute-api.us-east-1.amazonaws.com/
FastHarnessProxyStack.UserPoolId = us-east-1_XXXXXXXX
FastHarnessProxyStack.UserPoolClientId = XXXXXXXXXXXXXXXXXX
```

### 3. Cognito callback URLs

`cdk deploy` already configures `CallbackURLs`/`LogoutURLs` on the app client
(`http://localhost:3000` plus the Amplify Hosting URL — see [Production
Deployment](#production-deployment-amplify-hosting) below). You don't need to
run `aws cognito-idp update-user-pool-client` manually — doing so will be
overwritten on the next `cdk deploy` since CDK owns this configuration. Only
add URLs manually if you're using a custom domain not known at deploy time
(see step 3 under Production Deployment).

### 4. Configure the frontend

```bash
cp frontend/public/aws-exports.json.example frontend/public/aws-exports.json
```

Edit `frontend/public/aws-exports.json` with your deploy outputs:

```json
{
  "agentRuntimeArn": "https://XXXXXXXXXX.execute-api.us-east-1.amazonaws.com",
  "awsRegion": "us-east-1",
  "agentPattern": "strands-single-agent",
  "authority": "https://cognito-idp.us-east-1.amazonaws.com/<UserPoolId>",
  "client_id": "<UserPoolClientId>",
  "redirect_uri": "http://localhost:3000",
  "post_logout_redirect_uri": "http://localhost:3000",
  "response_type": "code",
  "scope": "email openid profile"
}
```

### 5. Create a user

```bash
aws cognito-idp admin-create-user \
  --user-pool-id <UserPoolId> \
  --username user@example.com \
  --user-attributes Name=email,Value=user@example.com \
  --temporary-password 'TempPass123!' \
  --region us-east-1

aws cognito-idp admin-set-user-password \
  --user-pool-id <UserPoolId> \
  --username user@example.com \
  --password 'YourPassword123!' \
  --permanent \
  --region us-east-1
```

### 6. Run the frontend

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:3000`, sign in, and chat with your harness agent.

---

## Production Deployment (Amplify Hosting)

`harness_proxy_stack.py` provisions an AWS Amplify Hosting app (ported from the
original FAST `infra-cdk/lib/amplify-hosting-construct.ts`) alongside the
Lambda proxy and Cognito pool. `cdk deploy` already creates the Amplify app,
staging S3 bucket, and Cognito callback/logout URLs for it — no separate
CloudFront setup is needed.

To make the UI accessible without running a local dev server:

1. Redeploy the stack if you haven't already (this creates the Amplify app):
   ```bash
   cd infra && source .env && cdk deploy
   ```
   Note the new outputs: `AmplifyUrl`, `AmplifyAppId`, `StagingBucketName`.

2. Deploy the built frontend to Amplify using the existing FAST script,
   pointing it at this stack:
   ```bash
   # From repo root
   python scripts/deploy-frontend.py FastHarnessProxyStack
   ```
   This builds the React app, generates `aws-exports.json` from the stack
   outputs, zips the build, uploads it to the staging bucket, and triggers
   an Amplify deployment. It prints the app URL when done
   (`https://main.<appId>.amplifyapp.com`).

3. Cognito callback/logout URLs for the Amplify domain are already configured
   by the CDK stack (`HarnessAppClient`). If you use a custom domain
   on Amplify later, add it explicitly:
   ```bash
   aws cognito-idp update-user-pool-client \
     --user-pool-id <UserPoolId> \
     --client-id <UserPoolClientId> \
     --callback-urls '["http://localhost:3000","https://main.<appId>.amplifyapp.com","https://your-custom-domain"]' \
     --logout-urls '["http://localhost:3000","https://main.<appId>.amplifyapp.com","https://your-custom-domain"]' \
     --allowed-o-auth-flows code \
     --allowed-o-auth-scopes openid email profile \
     --allowed-o-auth-flows-user-pool-client \
     --supported-identity-providers COGNITO \
     --region us-east-1
   ```

4. Restrict CORS in `harness_proxy_stack.py` to your Amplify domain in
   production (replace the `allow_origins=["*"]` in `HarnessProxyApi`).

---

## Project Structure

```
fast-harness/
├── frontend/                    # React chat UI (Vite + Tailwind + shadcn)
│   ├── public/
│   │   └── aws-exports.json.example  # Template for frontend config
│   └── src/
│       └── lib/agentcore-client/     # Modified streaming client → proxy
├── lambda/
│   └── harness_proxy/
│       ├── handler.py           # InvokeHarness → SSE translator
│       └── requirements.txt
├── infra/
│   ├── harness_proxy_stack.py       # CDK: Lambda + API GW + Cognito + Amplify
│   ├── amplify_hosting_construct.py # CDK: Amplify app + staging S3 bucket
│   ├── .env.example                 # Template for deployment config
│   ├── cdk.json
│   └── requirements.txt
├── infra-cdk/                   # Original FAST CDK (not used in this pattern)
└── docs/                        # Original FAST documentation (reference only —
                                  # describes the container-based deployment, not
                                  # this harness proxy pattern)
```

---

## How It Works

1. User sends a message in the React chat UI
2. Frontend sends POST to `<ApiUrl>/invoke` with JWT auth header
3. API Gateway validates the Cognito JWT
4. Lambda calls `invoke_harness()` with the user's message
5. Harness streams typed events (`contentBlockDelta`, etc.)
6. Lambda translates each event to SSE `data: {"data": "text"}` format
7. Frontend SSE parser renders the streamed text in the chat bubble

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `AccessDeniedException` on InvokeHarness | Lambda role needs `bedrock-agentcore:InvokeHarness` on your harness ARN |
| CORS errors in browser | Check API Gateway CORS matches your frontend origin |
| 401 from API Gateway | Verify Cognito JWT is valid and audience matches client ID |
| No response / timeout | Increase Lambda timeout; check if harness tools (MCP servers) are reachable |
| Sign-in button does nothing | Verify Cognito domain exists and callback URLs match your dev server port |
| "Site can't be reached" after login | Dev server isn't running or is on a different port |

---

## Adapting for Other Harness Agents

This pattern works for **any** AgentCore Harness agent. To point at a different agent:

1. Update `HARNESS_ID` in `infra/.env`
2. `source infra/.env && cd infra && cdk deploy`
3. Done — the Lambda will invoke the new harness

---

## License

This project is based on the [Fullstack AgentCore Solution Template](https://github.com/awslabs/fullstack-solution-template-for-agentcore) (Apache-2.0).
