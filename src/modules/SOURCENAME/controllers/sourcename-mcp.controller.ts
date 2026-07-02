// TEMPLATE: Replace SOURCENAME with your service name (e.g. datadog, pagerduty, github)
//           Replace all SOURCENAME occurrences. File rename: sourcename-mcp.controller.ts
import { Controller, Post, Body, HttpCode, HttpStatus, Logger } from '@nestjs/common';
import { ApiExcludeController } from '@nestjs/swagger';
import { SourcenameMcpService } from '../services/sourcename-mcp.service';
import type { McpJsonRpcRequest } from '../dto/mcp.dto';
import { jsonRpcSuccess, jsonRpcError, jsonRpcToolSuccess, jsonRpcToolError } from '../utils/json-rpc.utils';
import { toErrorMessage } from '../../../common/utils/error.utils';

/**
 * SourcenameMcpController
 *
 * Exposes SOURCENAME tools as an MCP JSON-RPC endpoint for Claude agents.
 *
 * Routes:
 *   POST /v1/sourcename/mcp             — JSON-RPC router (handles all MCP methods)
 *   POST /v1/sourcename/mcp/tools/list  — List available tool definitions
 *   POST /v1/sourcename/mcp/tools/call  — Execute a specific tool
 */
@ApiExcludeController()
@Controller({ path: 'sourcename/mcp', version: '1' })
export class SourcenameMcpController {
    private readonly logger = new Logger(SourcenameMcpController.name);

    constructor(private readonly mcpService: SourcenameMcpService) {}

    /**
     * JSON-RPC router — handles tools/list and tools/call in a single endpoint.
     * This is the canonical MCP entry point called by the batch server.
     */
    @Post()
    @HttpCode(HttpStatus.OK)
    async handleMcp(@Body() request: McpJsonRpcRequest): Promise<object> {
        const method: string = request.method ?? '';
        this.logger.log(`MCP request: ${method}`);

        try {
            if (method === 'tools/list') {
                return jsonRpcSuccess(request.id, { tools: this.mcpService.getToolDefinitions() });
            }
            if (method === 'tools/call') {
                return await this.dispatchToolCall(request);
            }
            if (method === 'initialize') {
                return jsonRpcSuccess(request.id, {
                    protocolVersion: '2024-11-05',
                    capabilities: { tools: {} },
                    serverInfo: { name: 'sourcename-mcp', version: '1.0.0' },
                });
            }
            if (method === 'notifications/initialized') {
                return {};
            }
            return jsonRpcError(request.id, -32601, `Method not found: ${method}`);
        } catch (err: unknown) {
            const msg = toErrorMessage(err);
            this.logger.error(`MCP error for method=${method}: ${msg}`);
            return jsonRpcError(request.id, this.extractErrorCode(err), msg || 'Internal server error');
        }
    }

    /** Dedicated tools/list endpoint. */
    @Post('tools/list')
    @HttpCode(HttpStatus.OK)
    listTools(@Body() request: McpJsonRpcRequest): object {
        return jsonRpcSuccess(request.id, { tools: this.mcpService.getToolDefinitions() });
    }

    /** Dedicated tools/call endpoint — called directly by the batch server per invocation. */
    @Post('tools/call')
    @HttpCode(HttpStatus.OK)
    async callTool(@Body() request: McpJsonRpcRequest): Promise<object> {
        return this.dispatchToolCall(request);
    }

    private async dispatchToolCall(request: McpJsonRpcRequest): Promise<object> {
        const params = request.params ?? {};
        const name = typeof params.name === 'string' ? params.name : '';
        const args = this.extractArgs(params.arguments);

        this.logger.log(`MCP tool call: ${name}`);

        try {
            const result = await this.mcpService.callTool(name, args);
            return jsonRpcToolSuccess(request.id, result);
        } catch (toolErr: unknown) {
            const msg = toErrorMessage(toolErr);
            this.logger.error(`MCP tool error: tool=${name}, error=${msg}`);
            // Tool errors must be returned as successful JSON-RPC responses with
            // isError:true per the MCP spec, not as JSON-RPC-level errors.
            return jsonRpcToolError(request.id, msg);
        }
    }

    private extractArgs(value: unknown): Record<string, unknown> {
        return value !== null && typeof value === 'object' ? (value as Record<string, unknown>) : {};
    }

    private extractErrorCode(err: unknown): number {
        if (err instanceof Error && 'status' in err) {
            return (err as unknown as { status: number }).status;
        }
        return -32603;
    }
}
