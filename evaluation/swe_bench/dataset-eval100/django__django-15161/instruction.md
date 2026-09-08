You are an expert software engineer maintaining the repository `django/django`.

The repository is checked out at /testbed, at commit `96e7ff5e9ff6362d9a886545869ce4496ca4b0fb`.

Here is the GitHub issue for this task:

<issue>
Use simplified paths for deconstruct of expressions
Description
	
Previously F() deconstructed to: django.db.models.expressions.F(). But since it can also be imported from django.db.models, ​PR #14047 changed it to deconstruct to django.db.models.F(). This simplifies generated migration code where it will be referenced only as from django.db import models / models.F().
As Mariusz pointed out on the PR, the same technique can be applied to other expressions, further simplifying generated migrations.

</issue>

Your task: modify the repository source code so the issue is resolved.

Rules:
- Do NOT edit test files to make tests pass; fix the source code.
- Work inside /testbed with the tools you have.
- The fix will be graded by running the project's test suite.
