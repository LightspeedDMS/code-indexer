//@ts-check
'use strict';
const { merge } = require('webpack-merge');
const nodeExternals = require('webpack-node-externals');
const TsconfigPathsPlugin = require('tsconfig-paths-webpack-plugin');

const config = merge({}, {
	resolve: {
		extensions: ['.js', '.json', '.ts'],
		// @ts-ignore
		plugins: [new TsconfigPathsPlugin({ configFile: './tsconfig.build.json' })],
	},
	performance: {
		hints: 'warning',
	},
	module: {
		rules: [
			{
				test: /\.ts$/,
				loader: 'ts-loader',
				exclude: /node_modules/,
			},
		],
	},
	target: 'node',
	node: {
		__dirname: true,
		__filename: true,
	},
	externals: [nodeExternals()],
});

module.exports = config;
