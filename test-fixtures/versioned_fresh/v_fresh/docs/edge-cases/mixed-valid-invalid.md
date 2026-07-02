# Document with Mixed Valid and Invalid Images

This document has a mix of valid and invalid image references for comprehensive testing.

## Valid Images (should be processed)

PNG format:
![Database Schema](../../images/database-schema.png)

JPEG format:
![API Flow](../../images/api-flow.jpg)

WebP format:
![Config Options](../../images/config-options.webp)

GIF format:
![Error Codes](../../images/error-codes.gif)

## Invalid Images (should be skipped with warnings)

Missing file:
![Missing](../../images/does-not-exist.png)

Remote URL:
![Remote](https://example.com/image.png)

Unsupported format:
![BMP](../../images/unsupported.bmp)

## Summary

- 4 valid images should be processed
- 3 invalid images should be skipped
- All text content should be indexed regardless
- Log should show 3 warnings for skipped images
