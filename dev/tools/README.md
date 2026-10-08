# Development Tools

Utility scripts used during development and testing.

## Scripts

- **`parse_test_results.py`**: reads the `test_summary.json` and per-test-file `.log` files that
  `full-automation.sh` writes to its output directory, and prints the failed files grouped by error type.
  Usage: `python3 dev/tools/parse_test_results.py <test_output_directory>`.
