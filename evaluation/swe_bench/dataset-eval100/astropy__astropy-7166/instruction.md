You are an expert software engineer maintaining the repository `astropy/astropy`.

The repository is checked out at /testbed, at commit `26d147868f8a891a6009a25cd6a8576d2e1bd747`.

Here is the GitHub issue for this task:

<issue>
InheritDocstrings metaclass doesn't work for properties
Inside the InheritDocstrings metaclass it uses `inspect.isfunction` which returns `False` for properties.

</issue>

Your task: modify the repository source code so the issue is resolved.

Rules:
- Do NOT edit test files to make tests pass; fix the source code.
- Work inside /testbed with the tools you have.
- The fix will be graded by running the project's test suite.
