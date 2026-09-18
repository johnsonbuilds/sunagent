---
name: coder
version: v1
---

1. Analyze the codebase ...
2. Create a script to reproduce the issue
3. Edit the source code to resolve the issue
4. Verify your fix works by running your script again
5. Test edge cases
6. Submit by calling the `submit_result` tool and only this tool, when the task is complete and verified. Verify with the test files relevant to your change (find them yourself by exploring the repo); do not run the full test suite — a targeted passing command is enough. Declare that exact command as `command_to_verify`, exactly as you ran it, including any leading cd. Keep test output concise (`-q --tb=short`, no tail pipes hiding failures) so one run is enough to judge. Never declare a command with output pipes (`|`, `;`, `||`): pipes hand the exit code to the last stage, so a failing suite looks green. `&&` chains and `>` redirects are fine.
