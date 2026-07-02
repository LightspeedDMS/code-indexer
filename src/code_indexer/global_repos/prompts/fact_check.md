You are verifying a dependency-map document against real source code.

Repositories to check against:
{repo_list}

(The file path and a relative-path hint will be appended by
_build_file_based_instructions — follow those instructions for opening and
editing the file.)

Your job:
1. Read the target file.
2. For every claim in the document (dependency, component name, integration
   point, etc.) use Read / Glob / Grep (and cidx tools if available) to
   verify it against the source code in the repos listed above.
3. If a claim is correct: leave it alone.
4. If a claim is wrong: use Edit to fix it.
5. If a claim cannot be verified (tools returned no evidence — not
   "I didn't have time"): use Edit to delete the surrounding sentence,
   bullet, or table row.
6. If you discover a real dependency or relationship that is missing AND
   you have concrete evidence (a file path + line range or a symbol
   definition location), use Edit to add it.
7. If you run out of turns before checking all claims: stop and print the
   sentinel anyway. Do NOT delete claims you did not have time to verify.

When you have finished editing, print exactly this line and stop:

FILE_EDIT_COMPLETE

Do not print anything else. No preamble, no summary, no JSON. Just edit
the file in place with your tools, then print that exact sentinel.
