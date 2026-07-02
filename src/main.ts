import { Logger } from '@nestjs/common';
import { bootstrapApplication } from './app';

async function bootstrap() {
    const { application } = await bootstrapApplication();
    const port = process.env.PORT || 3000;
    await application.listen(port);
    Logger.log(`Started application: http://localhost:${port}`);
}
void bootstrap();
