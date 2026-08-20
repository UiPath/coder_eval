"""The lint suite, split by what each rule reasons over.

A package, not a bare directory: `tests/` is a package and the rules import as
`tests.lint.*`, so these modules have to be importable by the same dotted path.
"""
