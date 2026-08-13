// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { AgentCoreConfig, AgentPattern, ChunkParser, StreamCallback } from "./types"
import { parseStrandsChunk } from "./parsers/strands"
import { parseLanggraphChunk } from "./parsers/langgraph"
import { parseClaudeAgentSdkChunk } from "./parsers/claude-agent-sdk"
import { parseAguiChunk } from "./parsers/agui"
import { readSSEStream } from "./utils/sse"

/** Resolve parser from pattern prefix. Defaults to strands parser. */
function getParser(pattern: AgentPattern): ChunkParser {
  if (pattern.startsWith("agui-")) return parseAguiChunk
  if (pattern.startsWith("langgraph-")) return parseLanggraphChunk
  if (pattern.startsWith("claude-")) return parseClaudeAgentSdkChunk
  if (pattern.startsWith("strands-")) return parseStrandsChunk
  return parseStrandsChunk
}

export class AgentCoreClient {
  private runtimeArn: string
  private region: string
  private pattern: AgentPattern
  private parser: ChunkParser

  constructor(config: AgentCoreConfig) {
    this.runtimeArn = config.runtimeArn
    this.region = config.region ?? "us-east-1"
    this.pattern = config.pattern
    this.parser = getParser(config.pattern)
  }

  generateSessionId(): string {
    return crypto.randomUUID()
  }

  async invoke(
    query: string,
    sessionId: string,
    accessToken: string,
    onEvent: StreamCallback
  ): Promise<void> {
    if (!accessToken) throw new Error("No valid access token found.")
    if (!this.runtimeArn) throw new Error("Agent Runtime ARN not configured.")

    // Proxy endpoint URL — loaded from aws-exports.json via the runtimeArn field
    // (repurposed as the proxy URL when using harness proxy pattern)
    const url = `${this.runtimeArn}/invoke`

    const body = {
      prompt: query,
      sessionId: sessionId,
    }

    // User identity is extracted server-side from the validated JWT token
    // (Authorization header), not sent in the payload body. This prevents
    // impersonation via prompt injection.
    
    const response = await fetch(url, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${accessToken}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    })



    if (!response.ok) {
      const errorText = await response.text()
      throw new Error(`HTTP ${response.status}: ${errorText}`)
    }

    await readSSEStream(response, this.parser, onEvent)
  }
}
