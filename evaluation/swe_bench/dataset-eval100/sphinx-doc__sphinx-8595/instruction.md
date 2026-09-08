You are an expert software engineer maintaining the repository `sphinx-doc/sphinx`.

The repository is checked out at /testbed, at commit `b19bce971e82f2497d67fdacdeca8db08ae0ba56`.

Here is the GitHub issue for this task:

<issue>
autodoc: empty __all__ attribute is ignored
**Describe the bug**
autodoc: empty `__all__` attribute is ignored

**To Reproduce**
```
# example.py
__all__ = []


def foo():
    "docstring"


def bar():
    "docstring"


def baz():
    "docstring"
```
```
# index.rst
.. automodule:: example
   :members:
```

All foo, bar, and baz are shown.

**Expected behavior**
No entries should be shown because `__all__` is empty.

**Your project**
No

**Screenshots**
No

**Environment info**
- OS: Mac
- Python version: 3.9.1
- Sphinx version: HEAD of 3.x
- Sphinx extensions: sphinx.ext.autodoc
- Extra tools: No

**Additional context**
No

</issue>

Your task: modify the repository source code so the issue is resolved.

Rules:
- Do NOT edit test files to make tests pass; fix the source code.
- Work inside /testbed with the tools you have.
- The fix will be graded by running the project's test suite.
