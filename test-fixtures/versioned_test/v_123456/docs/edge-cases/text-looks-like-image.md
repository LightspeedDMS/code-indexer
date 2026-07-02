# Document with Text That Looks Like Image Paths

This document contains text that resembles image paths but are NOT valid markdown image syntax.

## Not an Image Reference

The file is located at images/some-file.png in the repository.

You can find the screenshot at ../images/database-schema.png if you browse manually.

Path: /home/user/images/example.jpg

## Also Not Image References

- images/config.png
- ./images/test.gif
- "images/quoted-path.webp"

## This IS a Valid Image Reference

Only proper markdown syntax should be detected:

![Valid Reference](../../images/api-flow.jpg)

## Test Expectations

The parser should ONLY extract the one valid markdown image reference above.
Plain text paths should NOT be treated as image references.
