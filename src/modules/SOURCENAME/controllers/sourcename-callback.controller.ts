// TEMPLATE: Replace SOURCENAME/sourcename with your service name
import { Body, Controller, HttpCode, HttpStatus, Logger, Post } from '@nestjs/common';
import { ApiBody, ApiOperation, ApiTags } from '@nestjs/swagger';
import { SourcenameCallbackService } from '../services/sourcename-callback.service';
import type { CallbackPayloadDto } from '../dto/callback.dto';

/**
 * SourcenameCallbackController
 *
 * Internal endpoint called by the agent-router when a batch job targeting
 * a SOURCENAME resource completes (or fails). Posts the AI result to the
 * originating resource (comment, message, etc.).
 *
 * Expected metadata fields:
 *   - TODO: list the metadata fields your callback service reads
 *     e.g. issueKey: string, mrIid: number, channelId: string
 *
 * Version: '1' → registers at /v1/sourcename/callback
 */
@ApiTags('SOURCENAME Callback')
@Controller({ path: 'sourcename/callback', version: '1' })
export class SourcenameCallbackController {
    private readonly logger = new Logger(SourcenameCallbackController.name);

    constructor(private readonly callbackService: SourcenameCallbackService) {}

    @Post()
    @HttpCode(HttpStatus.OK)
    @ApiOperation({
        summary: 'Receive job completion callback from agent-router',
        description: 'Called by the agent-router after a batch job completes. ' + 'Posts the AI result to the originating SOURCENAME resource.',
    })
    @ApiBody({ schema: { type: 'object' } })
    async handleCallback(@Body() payload: CallbackPayloadDto): Promise<{ received: boolean }> {
        this.logger.log(`Callback received: status=${payload.status} job=${payload.jobId ?? 'n/a'} intent=${payload.intent}`);

        await this.callbackService.notify(payload);

        return { received: true };
    }
}
