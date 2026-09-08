You are an expert software engineer maintaining the repository `sympy/sympy`.

The repository is checked out at /testbed, at commit `3dff1b98a78f28c953ae2140b69356b8391e399c`.

Here is the GitHub issue for this task:

<issue>
sqf and sqf_list output is not consistant
The example below is wrong in the sense that we should have (x*_2 - 5_x + 6, 3) and not 2 factors of multiplicity 3.

```
>  sqf_list(  (x**2 + 1)  * (x - 1)**2 * (x - 2)**3 * (x - 3)**3  )

>  (1, [(x**2 + 1, 1), (x - 1, 2), (x - 3, 3), (x - 2, 3)])
```

whereas below is correct --- one factor of multiplicity 2

```
>  sqf_list( x**5 - 2*x**4 - 2*x**3 + 4*x**2 + x - 2 )

>  (1, [(x - 2, 1), (x**2 - 1, 2)])
```


</issue>

Your task: modify the repository source code so the issue is resolved.

Rules:
- Do NOT edit test files to make tests pass; fix the source code.
- Work inside /testbed with the tools you have.
- The fix will be graded by running the project's test suite.
