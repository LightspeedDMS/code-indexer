/**
 * JSON-RPC 2.0 response builder utilities for the MCP endpoint.
 * Pure functions with no dependencies — not a NestJS service.
 */

export function jsonRpcSuccess<T>(id: string | number | undefined, result: T): object {
    return { jsonrpc: '2.0', id, result };
}

export function jsonRpcError(id: string | number | undefined, code: number, message: string): object {
    return { jsonrpc: '2.0', id, error: { code, message } };
}

export function jsonRpcToolSuccess(id: string | number | undefined, content: unknown): object {
    return {
        jsonrpc: '2.0',
        id,
        result: {
            content: [{ type: 'text', text: JSON.stringify(content) }],
        },
    };
}

export function jsonRpcToolError(id: string | number | undefined, message: string): object {
    return {
        jsonrpc: '2.0',
        id,
        result: {
            content: [{ type: 'text', text: message || 'Tool execution failed' }],
            isError: true,
        },
    };
}
