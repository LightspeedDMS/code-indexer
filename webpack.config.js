//@ts-check
const path = require("path");
const slsw = require("serverless-webpack");
const { merge } = require("webpack-merge");
const TerserPlugin = require("terser-webpack-plugin");
const base = require("./webpack.base.js");

console.log({ stage: slsw.lib.options.stage });

const config = merge(base, {
	mode: "none",
	entry: slsw.lib.entries,
	devtool: "source-map",
	output: {
		libraryTarget: "commonjs",
		path: path.join(__dirname, ".webpack"),
		filename: '[name].js',
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
