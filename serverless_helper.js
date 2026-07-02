// TEMPLATE: Update credentialsSecretName and the MCP URL export name/description below.
const { log } = require("console");

module.exports = async ({ options, resolveVariable }) => {
	console.log('options', options);

	const region = await resolveVariable('opt:region');
	const regionMap = {
		'us-west-2': 'usw2',
		'us-east-1': 'use1',
	};
	const shortRegion = regionMap[region] || region.replace(/-/g, '');
	console.log('shortRegion', shortRegion);

	const stage = await resolveVariable('opt:stage');
	const alphanumericStage = stage.replace(/-/g, '');
	console.log('alphanumericStage', alphanumericStage);

	let result = {
		short_region: shortRegion,
		alphanumeric_stage: alphanumericStage,
	};

	// Try to get tags from SSM, return null if missing
	let defaultTagsParameter = await resolveVariable('ssm:/cicd/app/default_tags, null');
	let defaultTags = { Environment: "tools", Fallback: "true" };
	if (defaultTagsParameter) {
		try {
			defaultTags = JSON.parse(defaultTagsParameter);
		} catch (e) {
			console.log('Error parsing SSM tags, using fallback');
		}
	}
	const localTags = await resolveVariable("self:custom.tags, ''");
	result['tags'] = { ...defaultTags, ...localTags };

	// Optionally attach to a Terraform-managed API GW instead of creating a new one.
	// If /cicd/{service}/apig_id SSM param exists, Serverless references the existing
	// API GW (avoids apigateway:PUT for tagging a brand-new REST API on first deploy).
	// Falls back to '' so Serverless creates its own when the param is absent.
	const serviceName = await resolveVariable('self:service');
	let restApiId = '';
	let restApiRootResourceId = '';
	try {
		const apiGwId = await resolveVariable(`ssm:/cicd/${serviceName}/apig_id, null`);
		if (apiGwId) {
			// Validate apiGwId is safe to use in a shell command (API GW IDs are always alphanumeric).
			if (!/^[a-zA-Z0-9]+$/.test(apiGwId)) {
				throw new Error(`Unexpected apig_id format: ${apiGwId}`);
			}
			// Use the AWS CLI to avoid @aws-sdk/client-api-gateway version conflicts with
			// the @aws-sdk/util-user-agent-node override pinned in package.json overrides.
			const { execSync } = require('child_process');
			const rootId = execSync(
				`aws apigateway get-resources --rest-api-id ${apiGwId} --region ${region} --query "items[?path=='/'].id | [0]" --output text`,
				{ stdio: ['pipe', 'pipe', 'pipe'] },
			)
				.toString()
				.trim();
			if (rootId && rootId !== 'None') {
				restApiId = apiGwId;
				restApiRootResourceId = rootId;
				console.log(`apiGateway: using TF-managed API GW ${apiGwId}, root=${rootId}`);
			}
		}
	} catch (err) {
		console.warn(`apiGateway: TF-managed API GW lookup failed (${err.message}) — Serverless will create its own`);
	}
	result['restApiId'] = restApiId;
	result['restApiRootResourceId'] = restApiRootResourceId;

	const authorizerImportName = `general-lambda-authorizer-${stage}-${shortRegion}-arn`;
	let httpEvents;
	let cfResources;
	let cfOutputs;

	// TEMPLATE: Replace 'sourcename' in callback path, MCP URL description, and export name.
	if (restApiId) {
		httpEvents = [];
		cfResources = {
			ApiGatewayLambdaPermission: {
				Type: 'AWS::Lambda::Permission',
				Properties: {
					// TEMPLATE: Replace 'SOURCENAMEMcpLambdaFunction' with your function logical name
					// (Serverless generates it from the function key in serverless.yml, PascalCase + 'LambdaFunction')
					FunctionName: { 'Fn::GetAtt': ['SOURCENAMEMcpLambdaFunction', 'Arn'] },
					Action: 'lambda:InvokeFunction',
					Principal: 'apigateway.amazonaws.com',
					SourceArn: {
						'Fn::Sub': `arn:aws:execute-api:${region}:\${AWS::AccountId}:${restApiId}/*/*`,
					},
				},
			},
		};
		// TF-managed API GW stage is always 'default' (TF creates it; Serverless does not manage stages here)
		cfOutputs = {
			ServiceEndpoint: {
				Description: 'URL of the service endpoint',
				Value: `https://${restApiId}.execute-api.${region}.amazonaws.com/default`,
			},
			// TEMPLATE: Replace 'sourcename' in the key, description, and path below
			SourcenameMcpUrl: {
				Description: 'SOURCENAME MCP endpoint — set as SOURCENAME_MCP_URL in agent-router SSM (/cicd/agent-router/sourcename-mcp-url)',
				Value: `https://${restApiId}.execute-api.${region}.amazonaws.com/default/v1/sourcename/mcp`,
				Export: { Name: `${serviceName}-${stage}-${shortRegion}-mcp-url` },
			},
		};
	} else {
		const authConfig = {
			type: 'CUSTOM',
			authorizerId: { Ref: 'ApiGatewayAuthorizer' },
			resultTtlInSeconds: 300,
			identitySource: 'method.request.header.Authorization',
		};
		// TEMPLATE: Replace '/v1/sourcename/callback' with your callback path
		httpEvents = [
			{ http: { path: '/health', method: 'GET' } },
			{ http: { path: '/v1/sourcename/callback', method: 'POST' } },
			{ http: { path: '/{proxy+}', method: 'any', authorizer: authConfig } },
		];
		cfResources = {
			ApiGatewayAuthorizer: {
				Type: 'AWS::ApiGateway::Authorizer',
				Properties: {
					Name: `${serviceName}-authorizer-${stage}`,
					Type: 'TOKEN',
					IdentitySource: 'method.request.header.Authorization',
					AuthorizerUri: {
						'Fn::Sub': [
							'arn:aws:apigateway:${AWS::Region}:lambda:path/2015-03-31/functions/${AuthorizerArn}/invocations',
							{ AuthorizerArn: { 'Fn::ImportValue': authorizerImportName } },
						],
					},
					RestApiId: { Ref: 'ApiGatewayRestApi' },
					AuthorizerResultTtlInSeconds: 300,
				},
			},
			AuthorizerLambdaPermission: {
				Type: 'AWS::Lambda::Permission',
				Properties: {
					FunctionName: { 'Fn::ImportValue': authorizerImportName },
					Action: 'lambda:InvokeFunction',
					Principal: 'apigateway.amazonaws.com',
				},
			},
		};
		// TEMPLATE: Replace 'sourcename' in description, path, and export name
		cfOutputs = {
			SourcenameMcpUrl: {
				Description: 'SOURCENAME MCP endpoint — set as SOURCENAME_MCP_URL in agent-router SSM (/cicd/agent-router/sourcename-mcp-url)',
				Value: {
					'Fn::Sub': [
						`https://\${RestApiId}.execute-api.\${AWS::Region}.amazonaws.com/${stage}/v1/sourcename/mcp`,
						{ RestApiId: { Ref: 'ApiGatewayRestApi' } },
					],
				},
				Export: { Name: `${serviceName}-${stage}-${shortRegion}-mcp-url` },
			},
		};
	}
	result['httpEvents'] = httpEvents;
	result['cfResources'] = cfResources;
	result['cfOutputs'] = cfOutputs;

	// TEMPLATE: Update secret name pattern to match your service's Secrets Manager secret.
	// Pattern used by other spokes: claude-batch-server-{service}-{env}
	const envSuffix = stage.replace('tools-', '');
	result['credentialsSecretName'] = stage.startsWith('tools-')
		? `claude-batch-server-sourcename-mcp-${envSuffix}`
		: 'sourcename_mcp_credentials';

	return result;
};
