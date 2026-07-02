// TEMPLATE: Replace SOURCENAME/sourcename with your service name
import { Module } from '@nestjs/common';
import { ConfigModule } from '@nestjs/config';
import { SourcenameMcpController } from './controllers/sourcename-mcp.controller';
import { SourcenameCallbackController } from './controllers/sourcename-callback.controller';
import { SourcenameMcpService } from './services/sourcename-mcp.service';
import { SourcenameCallbackService } from './services/sourcename-callback.service';
import { SourcenameApiService } from './services/sourcename-api.service';

@Module({
    imports: [ConfigModule],
    controllers: [SourcenameMcpController, SourcenameCallbackController],
    providers: [SourcenameApiService, SourcenameMcpService, SourcenameCallbackService],
})
export class SourcenameModule {}
