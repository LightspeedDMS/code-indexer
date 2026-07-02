// TEMPLATE: Replace sourcename/SOURCENAME references with your service name
import { Test, TestingModule } from '@nestjs/testing';
import { INestApplication, VersioningType } from '@nestjs/common';
import request from 'supertest';
import { AppModule } from '../src/app.module';
import { LsAuthGuard } from '../src/common/guards/lsauth.guard';
import { MockGuard } from './mock-guard';

describe('SOURCENAME MCP Service (e2e)', () => {
    let app: INestApplication;

    beforeAll(() => {
        require('dotenv').config({ path: '.env.test' });
    });

    beforeEach(async () => {
        const moduleFixture: TestingModule = await Test.createTestingModule({
            imports: [AppModule],
        })
            .overrideGuard(LsAuthGuard)
            .useClass(MockGuard)
            .compile();

        app = moduleFixture.createNestApplication();
        app.enableVersioning({ type: VersioningType.URI, prefix: 'v' });
        await app.init();
    });

    afterEach(async () => {
        if (app) await app.close();
    });

    describe('Health', () => {
        it('/health (GET) — should return 200', () => {
            return request(app.getHttpServer())
                .get('/health')
                .expect(200)
                .expect((res) => {
                    expect(res.body).toHaveProperty('status');
                });
        });
    });

    describe('MCP endpoint', () => {
        it('POST /v1/sourcename/mcp — initialize', () => {
            return request(app.getHttpServer())
                .post('/v1/sourcename/mcp')
                .send({ jsonrpc: '2.0', id: 1, method: 'initialize', params: {} })
                .expect(200)
                .expect((res) => {
                    const body = res.body.payload ?? res.body;
                    expect(body.result).toHaveProperty('protocolVersion');
                    expect(body.result).toHaveProperty('capabilities');
                    expect(body.result.serverInfo.name).toBe('sourcename-mcp');
                });
        });

        it('POST /v1/sourcename/mcp — tools/list returns array', () => {
            return request(app.getHttpServer())
                .post('/v1/sourcename/mcp')
                .send({ jsonrpc: '2.0', id: 2, method: 'tools/list', params: {} })
                .expect(200)
                .expect((res) => {
                    const body = res.body.payload ?? res.body;
                    expect(Array.isArray(body.result.tools)).toBe(true);
                    expect(body.result.tools.length).toBeGreaterThan(0);
                });
        });

        it('POST /v1/sourcename/mcp — unknown method returns -32601', () => {
            return request(app.getHttpServer())
                .post('/v1/sourcename/mcp')
                .send({ jsonrpc: '2.0', id: 3, method: 'unknown/method', params: {} })
                .expect(200)
                .expect((res) => {
                    const body = res.body.payload ?? res.body;
                    expect(body.error.code).toBe(-32601);
                });
        });

        it('POST /v1/sourcename/mcp/tools/list — dedicated endpoint', () => {
            return request(app.getHttpServer())
                .post('/v1/sourcename/mcp/tools/list')
                .send({ jsonrpc: '2.0', id: 4 })
                .expect(200)
                .expect((res) => {
                    const body = res.body.payload ?? res.body;
                    expect(Array.isArray(body.result.tools)).toBe(true);
                });
        });
    });

    describe('Callback endpoint', () => {
        it('POST /v1/sourcename/callback — returns { received: true }', () => {
            return request(app.getHttpServer())
                .post('/v1/sourcename/callback')
                .send({
                    status: 'completed',
                    output: 'Test output',
                    source: 'sourcename',
                    intent: 'TEST',
                    metadata: { resourceId: 'test-resource-1' },
                })
                .expect(200)
                .expect((res) => {
                    const body = res.body.payload ?? res.body;
                    expect(body).toHaveProperty('received', true);
                });
        });
    });
});
