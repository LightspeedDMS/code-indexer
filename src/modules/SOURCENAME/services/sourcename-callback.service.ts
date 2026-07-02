// TEMPLATE: Replace SOURCENAME/sourcename with your service name.
//           Implement notify() to post the agent result to the originating platform resource.
import { Injectable, Logger } from '@nestjs/common';
import type { CallbackPayloadDto } from '../dto/callback.dto';
import { toErrorMessage } from '../../../common/utils/error.utils';

/**
 * SourcenameCallbackService
 *
 * Receives normalized callback payloads from the agent-router and posts
 * the AI analysis result back to the originating SOURCENAME resource
 * (e.g. a comment, a message, a status update).
 *
 * Required metadata fields (set by agent-router, validated in notify()):
 *   TODO: list required metadata fields, e.g.:
 *   - resourceId: string — the target resource to post back to
 */
@Injectable()
export class SourcenameCallbackService {
    private readonly logger = new Logger(SourcenameCallbackService.name);

    // TODO: Inject the API service or client needed to post results
    // constructor(private readonly apiService: SourcenameApiService) {}

    async notify(payload: CallbackPayloadDto): Promise<void> {
        // TODO: Extract the resource identifier(s) from metadata
        const resourceId = payload.metadata?.resourceId as string | undefined;

        if (!resourceId) {
            this.logger.error(`SOURCENAME callback missing resourceId in metadata — cannot post result. job=${payload.jobId ?? 'n/a'}`);
            return;
        }

        this.logger.log(`Callback received: resource=${resourceId} status=${payload.status} intent=${payload.intent} job=${payload.jobId ?? 'n/a'}`);

        if (payload.status === 'queued') {
            await this.handleQueued(resourceId, payload);
            return;
        }

        const comment = this.formatComment(payload);

        try {
            // TODO: Replace with actual API call to post result
            // await this.apiService.postResult(resourceId, comment);
            this.logger.log(`Posted callback result to resource=${resourceId}`);
            void comment; // remove when implemented
        } catch (err: unknown) {
            this.logger.error(`Failed to post SOURCENAME callback to resource=${resourceId}: ${toErrorMessage(err)}`);
        }
    }

    private handleQueued(resourceId: string, payload: CallbackPayloadDto): void {
        const comment = payload.output ?? `🔍 ${payload.intent.toUpperCase()} queued`;
        try {
            // TODO: Replace with actual API call
            // await this.apiService.postResult(resourceId, comment);
            this.logger.log(`Posted queued notification to resource=${resourceId} job=${payload.jobId ?? 'n/a'}`);
            void comment; // remove when implemented
        } catch (err: unknown) {
            this.logger.error(`Failed to post queued notification to resource=${resourceId}: ${toErrorMessage(err)}`);
        }
    }

    private formatComment(payload: CallbackPayloadDto): string {
        if (payload.status === 'completed') {
            return payload.output ?? '✅ Task completed.';
        }

        if (payload.status === 'error' && payload.errorType === 'validation') {
            const missing = payload.missingFields?.join(', ') ?? 'unknown fields';
            return `⚠️ ${payload.message ?? `Missing required fields: ${missing}`}`;
        }

        return `❌ ${payload.message ?? payload.output ?? 'Task failed — check logs for details.'}`;
    }
}
