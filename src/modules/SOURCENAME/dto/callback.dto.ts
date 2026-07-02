/**
 * Standardized callback payload received from the agent-router.
 *
 * The router normalizes both batch-server job completions and router
 * validation errors into this shape before dispatching to service callbacks.
 */
export interface CallbackPayloadDto {
    jobId?: string;
    status: 'queued' | 'completed' | 'failed' | 'error';
    errorType?: 'validation' | 'routing' | 'execution';
    output?: string;
    message?: string;
    missingFields?: string[];
    source: string;
    intent: string;
    playbookId?: string;
    /**
     * Original event data carried forward from the source message.
     * Add source-specific fields here with a comment explaining each.
     * Example:
     *   issueKey: string  — JIRA issue key
     *   mrIid:    number  — GitLab MR IID
     *   channelId: string — Slack channel ID
     */
    metadata?: Record<string, unknown>;
}
