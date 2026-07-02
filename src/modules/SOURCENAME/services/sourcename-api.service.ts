// TEMPLATE: Replace with the actual API client for your integration.
//           This service is the boundary between MCP tool handlers and the external API.
//           Keep all HTTP/SDK calls here — MCP service calls only this.
import { Injectable, Logger } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';

/**
 * SourcenameApiService
 *
 * Wraps the external SOURCENAME API (REST, SDK, etc.).
 * All credentials are read from environment variables via ConfigService.
 *
 * Credentials injected via SSM → serverless.yml environment:
 *   SOURCENAME_API_KEY      — API key (from /cicd/sourcename/api-key)
 *   SOURCENAME_API_BASE_URL — Base URL for the API (optional, has default)
 */
@Injectable()
export class SourcenameApiService {
    private readonly logger = new Logger(SourcenameApiService.name);
    private readonly apiKey: string;
    private readonly baseUrl: string;

    constructor(private readonly config: ConfigService) {
        this.apiKey = this.config.getOrThrow<string>('SOURCENAME_API_KEY');
        this.baseUrl = this.config.get<string>('SOURCENAME_API_BASE_URL') ?? 'https://api.sourcename.example.com';
    }

    // TODO: Implement actual API methods. Examples below are stubs.

    getResource(resourceId: string): Promise<Record<string, unknown>> {
        this.logger.log(`GET resource: ${resourceId}`);
        // TODO: implement actual API call
        // const response = await axios.get(`${this.baseUrl}/resources/${resourceId}`, {
        //     headers: { Authorization: `Bearer ${this.apiKey}` },
        // });
        // return response.data;
        return Promise.reject(new Error(`getResource not implemented for resourceId=${resourceId}`));
    }

    searchResources(query: string, maxResults: number): Promise<unknown[]> {
        this.logger.log(`Search resources: query="${query}" max=${maxResults}`);
        // TODO: implement actual API call
        return Promise.reject(new Error(`searchResources not implemented for query="${query}"`));
    }
}
