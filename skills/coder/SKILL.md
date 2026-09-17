---
name: coder
version: v1
---

1. Analyze the codebase ...
2. Create a script to reproduce the issue
3. Edit the source code to resolve the issue
4. Verify your fix works by running your script again
5. Test edge cases
6. Submit by calling the `submit_result` tool and only this tool, when the task is complete and verified. Verify incrementally to save time: run the test files relevant to your change first, and run the FULL suite once before submitting. Run the FULL suite once with `-rf` and redirect it to a file (`python -m pytest -q -rf > /tmp/full.log 2>&1`), then slice the file with grep — never re-run the suite just to change the filter. Never declare a command with output pipes (`|`, `;`, `||`): pipes hand the exit code to the last stage, so a failing suite looks green. `&&` chains and `>` redirects are fine. 
