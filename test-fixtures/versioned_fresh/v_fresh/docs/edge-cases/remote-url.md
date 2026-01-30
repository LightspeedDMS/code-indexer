# Document with Remote URL Image

This document references an image via remote URL which should be skipped.

## External Image Reference

The following image uses an HTTP URL and should be skipped:

![External Diagram](https://example.com/images/external-diagram.png)

And this one uses HTTPS:

![Secure External](https://cdn.example.org/assets/secure-image.jpg)

## Local Image for Comparison

This local image should still be processed:

![Local Error Codes](../../images/error-codes.gif)

## Test Expectations

- Remote URLs (http://, https://) should be skipped with warning
- Local relative paths should be processed normally
- Document text should still be searchable
