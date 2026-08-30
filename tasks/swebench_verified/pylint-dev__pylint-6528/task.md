I have access to a python code repository in the directory /testbed. You can explore and modify files using the available tools. Consider the following issue description:

<issue_description>
Pylint does not respect ignores in `--recursive=y` mode
### Bug description

Pylint does not respect the `--ignore`, `--ignore-paths`, or `--ignore-patterns` setting when running in recursive mode. This contradicts the documentation and seriously compromises the usefulness of recursive mode.

### Configuration

_No response_

### Command used

```shell
### .a/foo.py
# import re

### bar.py
# import re

pylint --recursive=y .
pylint --recursive=y --ignore=.a .
pylint --recursive=y --ignore-paths=.a .
pylint --recursive=y --ignore-patterns="^\.a" .
```


### Pylint output

All of these commands give the same output:

```
************* Module bar
bar.py:1:0: C0104: Disallowed name "bar" (disallowed-name)
bar.py:1:0: C0114: Missing module docstring (missing-module-docstring)
bar.py:1:0: W0611: Unused import re (unused-import)
************* Module foo
.a/foo.py:1:0: C0104: Disallowed name "foo" (disallowed-name)
.a/foo.py:1:0: C0114: Missing module docstring (missing-module-docstring)
.a/foo.py:1:0: W0611: Unused import re (unused-import)
```


### Expected behavior

`foo.py` should be ignored by all of the above commands, because it is in an ignored directory (even the first command with no ignore setting should skip it, since the default value of `ignore-patterns` is `"^\."`.

For reference, the docs for the various ignore settings from `pylint --help`:

```
    --ignore=<file>[,<file>...]
                        Files or directories to be skipped. They should be
                        base names, not paths. [current: CVS]
    --ignore-patterns=<pattern>[,<pattern>...]
                        Files or directories matching the regex patterns are
                        skipped. The regex matches against base names, not
                        paths. The default value ignores emacs file locks
                        [current: ^\.#]
    --ignore-paths=<pattern>[,<pattern>...]
                        Add files or directories matching the regex patterns
                        to the ignore-list. The regex matches against paths
                        and can be in Posix or Windows format. [current: none]
```

### Pylint version

```shell
pylint 2.13.7
python 3.9.12
```


### OS / Environment

_No response_

### Additional dependencies

_No response_

</issue_description>

Can you help me implement the necessary changes to the repository so that the requirements specified in the <issue_description> are met?
I've already taken care of all changes to any test files described in the <issue_description>. Do not modify test files. You may inspect and run tests, but all code changes must be limited to non-test implementation files.
Also the development Python environment is already set up for you (i.e., all dependencies already installed), so you don't need to install other packages.
Your task is to make the minimal changes to non-test files in the /testbed directory to ensure the <issue_description> is satisfied.
Prefer the smallest implementation change that satisfies the issue without changing unrelated behavior.
Use /testbed as the editable repository throughout the task.
Each shell command runs in a fresh shell. If a command needs the test environment, activate it in that same command, for example:
source /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && python -m pytest ...
Do not modify tests, test fixtures, generated expected outputs, or benchmark metadata.

Use the following workflow as a guide:

Phase 1. READING: understand the issue
   1.1 Identify key files, APIs, error messages, expected behavior, and reproduction details.
   1.2 Keep notes concise and focused on facts needed for the fix.
   1.3 Identify explicit questions, ambiguity, unresolved constraints, and tradeoffs in the issue description. Treat them as parts of the task to reason about, not optional commentary.

Phase 2. RUNNING: run relevant tests on the repository
   2.1 Use self-contained shell commands that activate the environment and then run the test or reproduction command.
   2.2 Run focused tests or commands related to the issue.
   2.3 Do not install packages or change environment/setup files.
   2.4 Iterate only on commands that provide useful evidence for the fix.

Phase 3. EXPLORATION: find the files that are related to the problem and possible solutions
   3.1 Use `grep` to search for relevant methods, classes, keywords and error messages.
   3.2 Identify all files related to the problem statement.
   3.3 Select the most likely implementation location and avoid unrelated refactors.

Phase 4. TEST CREATION: if useful, create a small temporary script to reproduce and verify the issue.
   4.1 Look at existing test files in the repository to understand the test format/structure.
   4.2 Prefer existing focused tests when they already cover the issue.
   4.3 If no focused test is available, create a minimal temporary reproduction script.
   4.4 Remove temporary scratch scripts before finishing.

Phase 5. FIX ANALYSIS: choose a focused fix
   5.1 State the concrete cause and the implementation change needed.
   5.2 Avoid broad behavior changes unless the issue requires them.

Phase 6. FIX IMPLEMENTATION: Edit the source code to implement your chosen solution.
   6.1 Make minimal, focused changes to fix the issue.

Phase 7. VERIFICATION: Test your implementation thoroughly.
   7.1 Run focused verification to confirm the fix works.
   7.2 Check relevant edge cases.
   7.3 Run existing tests related to the modified code to ensure you haven't broken anything.
   7.4 If a relevant existing test fails after your change, treat it as a likely regression unless you have concrete evidence it is unrelated.

Phase 8. FINAL REVIEW: Carefully re-read the problem description and compare your changes with the base commit 273a8b25620467c1e5686aa8d2a1dbb8c02c78d0.
   8.1 Ensure you've fully addressed all requirements.
   8.2 Run any tests in the repository related to:
     8.2.1 The issue you are fixing
     8.2.2 The files you modified
     8.2.3 The functions you changed
   8.3 If any tests fail, revise your implementation until all tests pass
   8.4 Before finishing, challenge your solution: name the most likely way it could still be incomplete or wrong based on the issue description, then run or write a focused check for that risk when practical.
   8.5 Inspect the final patch with `git diff -- .`.
   8.6 Do not leave temporary scripts, debug files, or scratch artifacts in the final patch.
   8.7 Keep only the minimal non-test source-code changes needed for the issue.

Be focused: gather enough evidence to make the correct minimal fix, then verify it with relevant tests.
