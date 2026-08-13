"""
FAST Harness Proxy Lambda
==========================
Translates AgentCore Harness typed event stream (messageStart, contentBlockDelta,
messageStop) into SSE data: lines that the FAST React frontend expects
(InvokeAgentRuntime format).

Architecture:
  FAST React Frontend → API Gateway → This Lambda → AgentCore Harness
  (expects SSE data: lines)                        (emits typed events)

The FAST frontend streaming client expects lines like:
  data: {"chunk": "Hello"}
  data: {"chunk": " world"}
  data: [DONE]

The Harness emits typed events:
  {"messageStart": {"role": "assistant"}}
  {"contentBlockDelta": {"delta": {"text": "Hello"}}}
  {"contentBlockDelta": {"delta": {"toolUse": {...}}}}
  {"messageStop": {"stopReason": "end_turn"}}

This Lambda bridges the two formats.
"""

import json
import logging
import os
import uuid
import time
import traceback
from typing import Any, Dict, Generator

import boto3
from botocore.config import Config

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

HARNESS_ARN = os.environ["HARNESS_ARN"]  # Required — set in CDK/Lambda env
RUNTIME_ARN = os.environ.get("RUNTIME_ARN", "")  # Optional — not used in invoke
REGION = os.environ.get("AWS_REGION", "us-east-1")
AGENT_NAME = os.environ.get("AGENT_NAME", "harness-agent")

# Session ID minimum length enforced by AgentCore
MIN_SESSION_ID_LENGTH = 33

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# --------------------------------------------------------------------------
# Boto3 Client (reused across warm invocations)
# --------------------------------------------------------------------------

_client = None


def get_agentcore_client():
    """Lazy-initialize the bedrock-agentcore client with retry config."""
    global _client
    if _client is None:
        config = Config(
            region_name=REGION,
            retries={"max_attempts": 3, "mode": "adaptive"},
            read_timeout=120,
            connect_timeout=10,
        )
        _client = boto3.client("bedrock-agentcore", config=config)
    return _client


# --------------------------------------------------------------------------
# Session ID Management
# --------------------------------------------------------------------------

def generate_session_id() -> str:
    """
    Generate a runtime session ID that meets the 33+ character requirement.
    Format: fast-{uuid4} → 5 + 36 = 41 chars (well over 33 minimum).
    """
    return f"fast-{uuid.uuid4()}"


def validate_session_id(session_id: str) -> str:
    """
    Validate session ID length. If too short, generate a compliant one.
    AgentCore requires runtimeSessionId to be at least 33 characters.
    """
    if not session_id or len(session_id) < MIN_SESSION_ID_LENGTH:
        logger.warning(
            f"Session ID '{session_id}' is too short (min {MIN_SESSION_ID_LENGTH} chars). "
            f"Generating a new one."
        )
        return generate_session_id()
    return session_id


# --------------------------------------------------------------------------
# Harness Event Stream → SSE Translation
# --------------------------------------------------------------------------

def translate_harness_event(event: Dict[str, Any]) -> list:
    """
    Translate a single Harness typed event into SSE data line(s).
    
    Returns a list of SSE lines (without the 'data: ' prefix) to emit.
    Some events produce zero lines (e.g., messageStart), some produce one.
    """
    sse_lines = []

    # --- Text content streaming ---
    if "contentBlockDelta" in event:
        delta = event["contentBlockDelta"].get("delta", {})
        
        # Plain text chunk
        if "text" in delta:
            sse_lines.append(json.dumps({
                "data": delta["text"]
            }))
        
        # Tool use events — pass through as structured JSON for frontend rendering
        elif "toolUse" in delta:
            tool_data = delta["toolUse"]
            sse_lines.append(json.dumps({
                "type": "tool_use",
                "toolUse": tool_data
            }))
    
    # --- Content block start (tool use begin) ---
    elif "contentBlockStart" in event:
        block_start = event["contentBlockStart"]
        if "start" in block_start and "toolUse" in block_start["start"]:
            tool_info = block_start["start"]["toolUse"]
            sse_lines.append(json.dumps({
                "type": "tool_use_start",
                "toolUseId": tool_info.get("toolUseId"),
                "name": tool_info.get("name")
            }))

    # --- Message start (role announcement) ---
    elif "messageStart" in event:
        role = event["messageStart"].get("role", "assistant")
        sse_lines.append(json.dumps({
            "type": "message_start",
            "role": role
        }))

    # --- Message stop (completion signal) ---
    elif "messageStop" in event:
        stop_reason = event["messageStop"].get("stopReason", "end_turn")
        sse_lines.append(json.dumps({
            "type": "message_stop",
            "stopReason": stop_reason
        }))
        # Emit the FAST-standard [DONE] sentinel
        sse_lines.append("[DONE]")

    # --- Metadata / usage ---
    elif "metadata" in event:
        # Optionally pass through usage stats
        sse_lines.append(json.dumps({
            "type": "metadata",
            "metadata": event["metadata"]
        }))

    # --- Unknown events — log and pass through ---
    else:
        logger.info(f"Unknown harness event type: {list(event.keys())}")
        sse_lines.append(json.dumps({
            "type": "unknown",
            "event": event
        }))

    return sse_lines


def invoke_harness_and_translate(
    prompt: str,
    session_id: str,
    conversation_history: list = None
) -> Generator[str, None, None]:
    """
    Call invoke_harness() and yield SSE-formatted lines.
    
    Each yielded string is a complete SSE line: "data: {json}\n\n"
    """
    client = get_agentcore_client()
    
    # Build the request payload
    request_params = {
        "harnessArn": HARNESS_ARN,
        "runtimeSessionId": session_id
    }
    
    # Build messages array in the Converse API format that Harness expects
    messages = []
    if conversation_history:
        messages.extend(conversation_history)
    
    # Add the current user message
    messages.append({
        "role": "user",
        "content": [{"text": prompt}]
    })
    request_params["messages"] = messages

    logger.info(
        f"Invoking harness: agent={AGENT_NAME}, session={session_id}, "
        f"prompt_length={len(prompt)}"
    )

    try:
        response = client.invoke_harness(**request_params)
    except client.exceptions.ValidationException as e:
        logger.error(f"Validation error from Harness: {e}")
        yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
        yield "data: [DONE]\n\n"
        return
    except client.exceptions.ThrottlingException as e:
        logger.error(f"Throttling from Harness: {e}")
        yield f"data: {json.dumps({'type': 'error', 'error': 'Rate limited. Please retry.'})}\n\n"
        yield "data: [DONE]\n\n"
        return
    except Exception as e:
        logger.error(f"Unexpected error invoking harness: {e}\n{traceback.format_exc()}")
        yield f"data: {json.dumps({'type': 'error', 'error': f'Harness invocation failed: {str(e)}'})}\n\n"
        yield "data: [DONE]\n\n"
        return

    # Process the event stream
    event_stream = response.get("output", {}).get("stream", response.get("stream", []))
    
    try:
        for event in event_stream:
            sse_lines = translate_harness_event(event)
            for line in sse_lines:
                yield f"data: {line}\n\n"
    except Exception as e:
        logger.error(f"Error processing event stream: {e}\n{traceback.format_exc()}")
        yield f"data: {json.dumps({'type': 'error', 'error': f'Stream processing error: {str(e)}'})}\n\n"
        yield "data: [DONE]\n\n"


# --------------------------------------------------------------------------
# Lambda Handler (API Gateway HTTP API integration)
# --------------------------------------------------------------------------

def handler(event, context):
    """
    Lambda handler for API Gateway HTTP API (v2 payload format).
    
    Expects POST body:
    {
        "prompt": "user message text",
        "sessionId": "optional-session-id-33-chars-min",
        "conversationHistory": []  // optional prior messages
    }
    
    Returns:
    - Streaming: API Gateway doesn't support true streaming from Lambda,
      so we collect all SSE lines and return them as a single response
      with content-type text/event-stream. The frontend EventSource or
      fetch+ReadableStream will parse the SSE lines.
    - For true streaming, use Lambda Function URL with response streaming
      (see README for configuration).
    """
    logger.info(f"Received event: {json.dumps({k: v for k, v in event.items() if k != 'body'})}")
    
    # --- Parse request body ---
    try:
        if event.get("isBase64Encoded"):
            import base64
            body_str = base64.b64decode(event.get("body", "")).decode("utf-8")
        else:
            body_str = event.get("body", "{}")
        
        body = json.loads(body_str) if body_str else {}
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"Failed to parse request body: {e}")
        return {
            "statusCode": 400,
            "headers": _cors_headers(),
            "body": json.dumps({"error": "Invalid JSON in request body"})
        }

    # --- Extract parameters ---
    prompt = body.get("prompt", "").strip()
    if not prompt:
        return {
            "statusCode": 400,
            "headers": _cors_headers(),
            "body": json.dumps({"error": "Missing 'prompt' in request body"})
        }

    session_id = validate_session_id(body.get("sessionId", ""))
    conversation_history = body.get("conversationHistory", [])

    # --- Invoke harness and collect SSE response ---
    sse_body_parts = []
    for sse_line in invoke_harness_and_translate(prompt, session_id, conversation_history):
        sse_body_parts.append(sse_line)

    sse_body = "".join(sse_body_parts)

    logger.info(f"Response: {len(sse_body_parts)} SSE lines, session={session_id}")

    return {
        "statusCode": 200,
        "headers": {
            **_cors_headers(),
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Session-Id": session_id,
        },
        "body": sse_body,
        "isBase64Encoded": False,
    }


def _cors_headers() -> dict:
    """Standard CORS headers for API Gateway responses."""
    return {
        "Access-Control-Allow-Origin": "*",  # Restrict in production
        "Access-Control-Allow-Headers": "Content-Type,Authorization,X-Session-Id",
        "Access-Control-Allow-Methods": "POST,OPTIONS",
        "Access-Control-Expose-Headers": "X-Session-Id",
    }
