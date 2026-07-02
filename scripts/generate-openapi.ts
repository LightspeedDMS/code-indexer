import { SwaggerModule, DocumentBuilder } from '@nestjs/swagger';
import { writeFileSync } from 'fs';
import { join } from 'path';
import * as yaml from 'js-yaml';
import { bootstrapApplication } from '../src/app';

async function generateOpenApiSpec() {
    try {
        const { application } = await bootstrapApplication();

        const packageJson = require('../package.json');

        const config = new DocumentBuilder()
            .setTitle(packageJson.name || 'API Documentation')
            .setDescription(packageJson.description || 'Generated API documentation')
            .setVersion(packageJson.version || '1.0.0')
            .addBearerAuth(
                {
                    type: 'http',
                    scheme: 'bearer',
                    bearerFormat: 'JWT',
                    description: 'JWT token obtained from Lightspeed authentication service',
                },
                'LsAuth',
            )
            .build();

        const document = SwaggerModule.createDocument(application, config, {
            operationIdFactory: (controllerKey: string, methodKey: string) => {
                let version = '';
                if (controllerKey.includes('Controller')) {
                    const match = controllerKey.match(/v(\d+)/i);
                    if (match) version = `v${match[1]}-`;
                }
                return `${version}${controllerKey}-${methodKey}`;
            },
        });

        ensureUniqueOperationIds(document);

        const jsonPath = join(__dirname, '..', 'docs', 'openapi.json');
        writeFileSync(jsonPath, JSON.stringify(document, null, 2));
        console.log(`OpenAPI JSON generated: ${jsonPath}`);

        const yamlPath = join(__dirname, '..', 'docs', 'openapi.yaml');
        writeFileSync(yamlPath, yaml.dump(document, { indent: 2, lineWidth: -1 }));
        console.log(`OpenAPI YAML generated: ${yamlPath}`);

        await application.close();
    } catch (error) {
        console.error('Failed to generate OpenAPI specification:', error);
        process.exit(1);
    }
}

function ensureUniqueOperationIds(document: any) {
    const operationIds = new Set<string>();
    const duplicates = new Map<string, number>();

    for (const pathValue of Object.values(document.paths)) {
        for (const operation of Object.values(pathValue as any)) {
            if (typeof operation === 'object' && operation && 'operationId' in operation) {
                const op = operation as any;
                if (operationIds.has(op.operationId)) {
                    const count = duplicates.get(op.operationId) || 1;
                    duplicates.set(op.operationId, count + 1);
                } else {
                    operationIds.add(op.operationId);
                }
            }
        }
    }

    const counters = new Map<string, number>();
    for (const pathValue of Object.values(document.paths)) {
        for (const operation of Object.values(pathValue as any)) {
            if (typeof operation === 'object' && operation && 'operationId' in operation) {
                const op = operation as any;
                if (duplicates.has(op.operationId)) {
                    const baseId = op.operationId;
                    const counter = (counters.get(baseId) || 0) + 1;
                    counters.set(baseId, counter);
                    op.operationId = `${baseId}_${counter}`;
                }
            }
        }
    }
}

generateOpenApiSpec();
