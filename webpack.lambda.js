//@ts-check
const path = require("path");
const { merge } = require("webpack-merge");
const TerserPlugin = require("terser-webpack-plugin");
const base = require("./webpack.base.js");

// Standalone Lambda webpack build — used by Dockerfile (not serverless-webpack).
// Entry is hardcoded to src/serverless.ts rather than slsw.lib.entries.

const config = merge(base, {
	mode: "production",
	entry: path.resolve(__dirname, 'src/serverless.ts'),
	devtool: "source-map",
	output: {
		libraryTarget: "commonjs",
		path: path.join(__dirname, ".webpack"),
		filename: 'serverless.js',
	},
	optimization: {
		minimize: true,
		minimizer: [
			new TerserPlugin({
				terserOptions: {
					compress: {
						drop_console: false,
						drop_debugger: true,
					},
				},
			}),
		],
	},
});

module.exports = config;
