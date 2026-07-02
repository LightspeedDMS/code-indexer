#!/usr/bin/env node
/**
 * MCP Server Wrapper — stdio bridge for local development
 *
 * TEMPLATE: Update SERVICE_URL, server name in initialize response, and env var names.
 *
 * Implements the MCP stdio JSON-RPC transport and forwards requests to the
 * local NestJS service running at http://localhost:3000.
 *
 * The service must be running: npm run start:dev
 *
 * Usage (in ~/.claude/mcp.json):
 *   "sourcename-mcp": {
 *     "command": "node",
 *     "args": ["/path/to/sourcename-mcp/mcp-server-wrapper.js"]
 *   }
 */

const readline = require('readline');
const http = require('http');

// TEMPLATE: Update path to match your MCP controller route
const SERVICE_URL = 'http://localhost:3000/v1/sourcename/mcp/tools/call';

// ─── MCP Protocol Handler ──────────────────────────────────────────────────

async function callService(toolName, args) {
    return new Promise((resolve, reject) => {
        const requestBody = JSON.stringify({
            jsonrpc: '2.0',
            id: Date.now(),
            method: 'tools/call',
            params: { name: toolName, arguments: args },
        });

        const req = http.request(
            SERVICE_URL,
            {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Content-Length': Buffer.byteLength(requestBody),
                },
            },
            (res) => {
                let data = '';
                res.on('data', (chunk) => { data += chunk; });
                res.on('end', () => {
                    try {
                        const response = JSON.parse(data);
                        // Handle NestJS ResponseModule wrapper: { metadata, payload }
                        if (response.payload?.result) {
                            resolve(response.payload.result);
                        } else if (response.result) {
                            resolve(response.result);
                        } else if (response.error) {
                            reject(new Error(response.error.message || 'Service error'));
                        } else {
                            resolve(response);
                        }
                    } catch (error) {
                        reject(new Error(`Failed to parse response: ${error.message}`));
                    }
                });
            }
        );

        req.setTimeout(30_000, () => {
            req.destroy(new Error('HTTP request timed out after 30s. Is the service running? Run: npm run start:dev'));
        });

        req.on('error', (error) => {
            reject(new Error(`HTTP request failed: ${error.message}. Run: npm run start:dev`));
        });

        req.write(requestBody);
        req.end();
    });
}

async function handleMessage(message) {
    const { method, id, params } = message;

    // MCP notifications (no id) must not receive a response
    if (id === undefined || id === null) {
        return null;
    }

    try {
        switch (method) {
            case 'initialize':
                return {
                    jsonrpc: '2.0',
                    id,
                    result: {
                        protocolVersion: '2024-11-05',
                        capabilities: { tools: {}, logging: {} },
                        // TEMPLATE: Update server name
                        serverInfo: { name: 'sourcename-mcp', version: '1.0.0' },
                    },
                };

            case 'tools/list': {
                // Fetch tool list from the running service
                const listBody = JSON.stringify({ jsonrpc: '2.0', id: Date.now(), method: 'tools/list', params: {} });
                const tools = await new Promise((resolve, reject) => {
                    const req = http.request(
                        // TEMPLATE: Update path
                        'http://localhost:3000/v1/sourcename/mcp/tools/list',
                        { method: 'POST', headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(listBody) } },
                        (res) => {
                            let data = '';
                            res.on('data', (c) => { data += c; });
                            res.on('end', () => {
                                try {
                                    const r = JSON.parse(data);
                                    const payload = r.payload ?? r;
                                    resolve(payload.result?.tools ?? []);
                                } catch (e) { reject(e); }
                            });
                        }
                    );
                    req.on('error', reject);
                    req.write(listBody);
                    req.end();
                });
                return { jsonrpc: '2.0', id, result: { tools } };
            }

            case 'tools/call': {
                const { name, arguments: args } = params;
                try {
                    const result = await callService(name, args || {});
                    return { jsonrpc: '2.0', id, result };
                } catch (toolError) {
                    return {
                        jsonrpc: '2.0',
                        id,
                        result: { content: [{ type: 'text', text: `Error: ${toolError.message}` }], isError: true },
                    };
                }
            }

            default:
                return { jsonrpc: '2.0', id, error: { code: -32601, message: `Method not found: ${method}` } };
        }
    } catch (error) {
        return { jsonrpc: '2.0', id, error: { code: -32603, message: error.message, data: { stack: error.stack } } };
    }
}

async function main() {
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout, terminal: false });

    rl.on('line', async (line) => {
        const trimmed = line.trim();
        if (!trimmed) return;
        try {
            const message = JSON.parse(trimmed);
            const response = await handleMessage(message);
            if (response !== null) {
                process.stdout.write(JSON.stringify(response) + '\n');
            }
        } catch (error) {
            process.stdout.write(
                JSON.stringify({ jsonrpc: '2.0', id: null, error: { code: -32700, message: 'Parse error', data: { originalError: error.message } } }) + '\n'
            );
        }
    });

    process.on('SIGINT', () => process.exit(0));
    process.on('SIGTERM', () => process.exit(0));
}

main().catch((error) => {
    process.stderr.write(`Fatal error: ${error.message}\n`);
    process.exit(1);
});
