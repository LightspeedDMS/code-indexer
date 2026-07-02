// TEMPLATE: Replace SOURCENAME/sourcename with your service name.
//           Replace SourcenameApiService with your actual API client service.
import { Injectable, Logger } from '@nestjs/common';
import { SourcenameApiService } from './sourcename-api.service';

// ─── Tool Definitions ──────────────────────────────────────────────────────
//
// Each entry must have:
//   name:        snake_case, unique, matches handler key in this.handlers map
//   description: agent-facing — be specific about what the tool does and when to use it
//   inputSchema: JSON Schema for the arguments object
//
// Guidelines:
//   - Add response_mode (minimal/summary/full) for tools that can return large payloads
//   - Add _guidance fields to responses to steer agent behaviour
//   - Keep descriptions concise but precise — agents read these during tool selection

const TOOL_DEFINITIONS = [
    // TODO: Replace these examples with real tools for SOURCENAME
    {
        name: 'example_get',
        description: 'Fetch a SOURCENAME resource by ID.',
        inputSchema: {
            type: 'object',
            properties: {
                resource_id: {
                    type: 'string',
                    description: 'The resource identifier',
                },
                response_mode: {
                    type: 'string',
                    enum: ['minimal', 'summary', 'full'],
                    description: 'Response detail level (default: summary)',
                    default: 'summary',
                },
            },
            required: ['resource_id'],
        },
    },
    {
        name: 'example_search',
        description: 'Search SOURCENAME resources.',
        inputSchema: {
            type: 'object',
            properties: {
                query: {
                    type: 'string',
                    description: 'Search query',
                },
                max_results: {
                    type: 'number',
                    description: 'Maximum results to return (default: 20)',
                    default: 20,
                },
            },
            required: ['query'],
        },
    },
] as const;

// ─── Service ───────────────────────────────────────────────────────────────

/**
 * SourcenameMcpService
 *
 * Implements MCP tool handlers for the SOURCENAME integration.
 * Uses a handler map pattern for clean dispatch and easy extension.
 *
 * Pattern:
 *   1. Define tools in TOOL_DEFINITIONS above
 *   2. Register a handler in this.handlers (constructor)
 *   3. Implement a private handleXxx() method
 *   4. Delegate actual API calls to SourcenameApiService
 */
@Injectable()
export class SourcenameMcpService {
    private readonly logger = new Logger(SourcenameMcpService.name);

    private readonly handlers: Map<string, (args: Record<string, unknown>) => Promise<unknown>>;

    constructor(private readonly apiService: SourcenameApiService) {
        this.handlers = new Map([
            // TODO: Wire each tool name to its handler
            ['example_get', (args) => this.handleExampleGet(args)],
            ['example_search', (args) => this.handleExampleSearch(args)],
        ]);
    }

    getToolDefinitions() {
        return TOOL_DEFINITIONS;
    }

    async callTool(name: string, args: Record<string, unknown>): Promise<unknown> {
        this.logger.log(`MCP tool call: ${name}`);

        const handler = this.handlers.get(name);
        if (!handler) {
            throw new Error(`Unknown SOURCENAME MCP tool: ${name}`);
        }

        return handler(args);
    }

    // ─── Handlers ────────────────────────────────────────────────────────

    // TODO: Replace these stubs with real implementations

    private async handleExampleGet(args: Record<string, unknown>): Promise<unknown> {
        const resourceId = args.resource_id as string;
        const mode = (args.response_mode as string | undefined) ?? 'summary';

        const resource = await this.apiService.getResource(resourceId);

        return {
            ...resource,
            _mode: mode,
            _guidance: {
                message: 'Resource retrieved.',
                suggestions: ['Call example_search to find related resources'],
            },
        };
    }

    private async handleExampleSearch(args: Record<string, unknown>): Promise<unknown> {
        const query = args.query as string;
        const maxResults = (args.max_results as number | undefined) ?? 20;

        const results = await this.apiService.searchResources(query, maxResults);

        return {
            total: results.length,
            results,
            _guidance: {
                message: `Found ${results.length} results.`,
                suggestions: ['Call example_get with a specific resource_id for details'],
            },
        };
    }
}
