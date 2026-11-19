## Summary

The grader expects a literal the task prompt never asks for. This matches the
documented output instead. See https://github.com/UiPath/skills/pull/123 and
[the build](https://dev.azure.com/example-org/example-project/_build/results?buildId=1000001).

- [an off-list link](https://example.invalid/phish)
- [a script link](javascript:alert(1))
- ![an image](https://example.invalid/pixel.png)

<b>raw html</b>

| Before | After |
| --- | --- |
| fail | pass |
