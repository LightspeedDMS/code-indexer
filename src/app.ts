import { NestFactory } from '@nestjs/core';
import { AppModule } from './app.module';
import { ValidationPipe, VersioningType } from '@nestjs/common';
import helmet from 'helmet';
import { Logger, LoggerErrorInterceptor } from 'nestjs-pino';
import { Request, Response, NextFunction } from 'express';

export async function bootstrapApplication() {
    const application = await NestFactory.create(AppModule);
    application.useLogger(application.get(Logger));
    application.enableVersioning({
        type: VersioningType.URI,
        prefix: 'v',
    });
    application.useGlobalInterceptors(new LoggerErrorInterceptor());
    application.use(helmet());

    // Simulate Lambda timeout locally
    const lambdaTimeoutMs = parseInt(process.env.LAMBDA_TIMEOUT_MS ?? '0', 10);
    if (lambdaTimeoutMs > 0) {
        application.use((req: Request, res: Response, next: NextFunction) => {
            req.setTimeout(lambdaTimeoutMs, () => {
                if (!res.headersSent) {
                    res.status(504).json({ error: 'Simulated Lambda timeout', timeoutMs: lambdaTimeoutMs });
                }
            });
            next();
        });
    }

    application.useGlobalPipes(new ValidationPipe({ transform: true, whitelist: true }));

    return { application };
}
