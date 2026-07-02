# Special Markdown Image Syntax

This document tests various markdown image syntax variants.

## Image with Title Attribute

![Database Schema](../../images/database-schema.png "Database table structure")

## Image with Empty Alt Text

![](../../images/api-flow.jpg)

## Image with Special Characters in Alt Text

![User's "Profile" & Settings <Config>](../../images/config-options.webp)

## Escaped Image Syntax (Should NOT Extract)

This is escaped and should not be extracted:
\![Not an image](../../images/error-codes.gif)

## Reference-Style Images

Here is a reference-style image: ![Schema Reference][schema-ref]

And another one: ![API Flow][api-ref]

[schema-ref]: ../../images/database-schema.png
[api-ref]: ../../images/api-flow.jpg "API authentication flow"

## Multiple Images on Same Line

![First](../../images/database-schema.png) and ![Second](../../images/api-flow.jpg) on same line.

## Image Inside Link (Clickable Image)

[![Clickable Config](../../images/config-options.webp)](https://example.com/config)

## Test Expectations

Standard inline images should extract:
- database-schema.png (with title)
- api-flow.jpg (empty alt)
- config-options.webp (special chars in alt)

Reference-style images (if supported):
- database-schema.png (via schema-ref)
- api-flow.jpg (via api-ref)

Multiple on same line:
- database-schema.png
- api-flow.jpg

Clickable image:
- config-options.webp

Should NOT extract:
- error-codes.gif (escaped syntax)
