// TEMPLATE: Replace SourcenameModule with your module import
import { Module } from '@nestjs/common';
import { APP_FILTER } from '@nestjs/core';
import { LoggerModule } from 'nestjs-pino';
import { getCurrentInvoke } from '@vendia/serverless-express';
import { ConfigModule } from '@nestjs/config';
import pino from 'pino';
import { AllExceptionFilter, HttpExceptionFilter, ResponseModule, HealthModule, HealthTier, createMemoryHealth } from '@lightspeed/nestjs-common-components';
import { SourcenameModule } from './modules/SOURCENAME/sourcename.module';

@Module({
    imports: [
        ResponseModule,
        HealthModule.register([createMemoryHealth({ tier: HealthTier.BASIC }, 1024, 'memory')], {
            enableWildcard: true,
        }),
        ConfigModule.forRoot({ isGlobal: true }),
        SourcenameModule,
        LoggerModule.forRoot({
            pinoHttp: {
                timestamp: pino.stdTimeFunctions.isoTime,
                level: 'trace',
                genReqId: (request) => {
                    return getCurrentInvoke().event?.requestContext?.requestId || request.id;
                },
                serializers: {
                    req: (request) => ({ id: request?.id, url: request?.url, method: request?.method }),
                    res: (response) => ({ statusCode: response?.statusCode }),
                    err: (error) => ({
                        statusCode: error?.status,
                        type: error?.type,
                        message: error?.message,
                        stack: error?.stack,
                    }),
                },
                customAttributeKeys: { req: 'request', res: 'response', err: 'error' },
                formatters: {
                    level: (level) => ({ level }),
                },
                customLogLevel: (_request, response, error) => {
                    if (response.statusCode >= 400 && response.statusCode < 500) return 'error';
                    if (response.statusCode >= 500 || error) return 'fatal';
                    return 'info';
                },
            },
        }),
    ],
    providers: [
        { provide: APP_FILTER, useClass: HttpExceptionFilter },
        { provide: APP_FILTER, useClass: AllExceptionFilter },
    ],
})
export class AppModule {}
