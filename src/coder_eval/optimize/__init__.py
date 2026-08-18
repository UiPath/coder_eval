"""The ``/coder-eval:optimize-skill`` decision family: ranks 0-3, plus the ladder-exempt sidecar.

``load`` (0) reads a finalized run tree and decides nothing; ``gate`` (1) owns the primitives both
tracks share; ``activation`` and ``execution`` (2) are the two gates, beside each other and
forbidden from importing each other; ``fronts`` and ``search`` (3) sit above them. ``store`` is the
``measurements.json`` sidecar every rank may import and is deliberately outside the ladder.

**The ranks are declared in exactly one place** — ``_OPTIMIZE_RANKS`` in the layering tests, beside
a ``pkgutil``-derived set of this package's modules and a coverage assert binding the two, so a new
module here cannot join the family unranked. They are a SPECIFICATION, not a derivation:
``activation -> execution`` is acyclic and still wrong, so a rank computed from the import graph
would pass over the exact violation the ladder exists to forbid.

**This package re-exports nothing, on purpose.** A facade ``__init__`` would let every pre-package
import keep working and make the split cosmetic, which is precisely what
``test_a_moved_name_lives_in_exactly_one_module`` forbids one module over. Two sensors hold it:
``test_the_optimize_package_reexports_nothing`` asserts this file binds no non-module public name,
and ``test_no_flat_shim_module_survives_the_move`` asserts no module answers at an old flat path.

**Inside this package a name shared between siblings is public.** Only a file-local name keeps a
leading underscore, because one underscore cannot mark two boundaries — a helper four modules
import, spelled like a module-local one, tells a reader "safe to change this signature" about the
opposite. The skill-facing surface is not marked at all; it is the set the ``SKILL.md`` snippet
binder resolves.
"""
