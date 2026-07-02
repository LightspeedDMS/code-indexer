/**
 * DTO for incoming JSON-RPC 2.0 requests to the MCP endpoint.
 */
export interface McpJsonRpcRequest {
    jsonrpc?: string;
    method?: string;
    id?: string | number;
    params?: Record<string, unknown>;
}
