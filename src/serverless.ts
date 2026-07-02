import { APIGatewayProxyCallback, APIGatewayProxyEvent, APIGatewayProxyHandler, Context } from 'aws-lambda';
import serverlessExpress from '@vendia/serverless-express';
import { bootstrapApplication } from './app';

let lambdaInstance: APIGatewayProxyHandler | undefined;

async function bootstrapLambda(): Promise<APIGatewayProxyHandler> {
    if (!lambdaInstance) {
        console.log('LOG => Initializing Lambda API instance');

        const { application } = await bootstrapApplication();
        await application.init();

        const expressApp = application.getHttpAdapter().getInstance();

        lambdaInstance = serverlessExpress({ app: expressApp });
    }
    return lambdaInstance;
}

async function handler(event: APIGatewayProxyEvent, context: Context, callback: APIGatewayProxyCallback) {
    context.callbackWaitsForEmptyEventLoop = false;

    // Workaround: API Gateway sets requestContext.path with the stage prefix but event.path strips
    // the stage. Overwrite proxy param so serverless-express forwards the correct path to NestJS.
    if (event.pathParameters) {
        event.pathParameters.proxy = event.path.replace(/^\//, '');
    }

    const server = await bootstrapLambda();
    return server(event, context, callback);
}

module.exports.handler = handler;
