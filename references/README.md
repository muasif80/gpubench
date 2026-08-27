# references/

Reference documentation for the parts of gpubench that other people's code has to interoperate
with. One file per contract, written to be read by someone who has never seen this repository.

| File | What it specifies |
|---|---|
| `checks.md` | The claims manifest (`schema: "claims/1"`) and all 30 checks the pre-render gate enforces over it. Per check: severity, jurisdiction (manifest, rendered document, or external result artefact), what it catches, how to satisfy it, and what it cannot see. Includes the full manifest schema with defaults, a minimal valid manifest, and the same manifest with one defect beside the verifier's real output. |

## Why this directory exists

`gpubench/verify.py` enforces 29 checks and `gpubench/longform/__init__.py` adds one more, and until
now the only description of any of them was the source. That blocks the thing the manifest is
ultimately for: someone else emitting a compatible manifest and being judged by the same rules. A
result format nobody can read is not a standard, it is an implementation.

Rather than a rulebook, these files aim to be usable in one sitting: what to emit, what will be
rejected, and where each check is blind. A gate's blind spots are part of its contract. A reader
who plans around a check without knowing what it cannot see is worse off than one who does not use
the check at all.

## What belongs here, and what does not

**Belongs here:** the shape of a file another program writes or reads, and the rules applied to it.

**Does not belong here:**

- Prose about a particular machine or a particular measurement. The tool owns engines, formats,
  templates and the gate. It owns no content. Content lives in the repository that publishes the
  report.
- The report template linter. `gpubench/template/` is a **separate** contract (a run bundle plus a
  rendered report, rule ids L1 to L11, its own eight-member `kind` enum) and it is already
  documented next to itself in `gpubench/template/lint-rules.md` and
  `gpubench/template/README.md`. `checks.md` says explicitly where the two vocabularies differ,
  because they share words and are not interchangeable.
- Anything generated. Everything here is written by hand and checked against the code.

## Keeping these honest

Two rules, learned the hard way. Every hole two adversarial audits found in the gate was a check
that read a declaration instead of the artifact, and a document that describes a gate is itself a
declaration.

1. **Read the code, not the last version of the document.** When a check changes, re-read the
   function and re-run the command before editing the entry. `checks.md` quotes finding text
   verbatim from real runs and names the command that produced each quote, so any claim in it can
   be re-tested in a few seconds.
2. **Cross-check both directions.** For every id in the document, confirm it exists in the code;
   for every id in the code, confirm the document has an entry. `checks.md` ends with that
   cross-check, and it is meant to be redone, not trusted.

## Known gaps, as of the first edition of `checks.md`

Recorded here because a reference document that hides what it is unsure of is the failure mode it
exists to prevent.

- **9 of the 30 check ids have no id-named assertion** in `tests/test_verify.py`,
  `tests/test_gate.py` or `tests/test_attacks.py`: A2, C1, C2, D1, D5, E1, F1, G1, G2. Five of the
  nine fire in `gpubench verify --demo`, which is a command a person runs rather than a test. Four
  (C2, D1, D5, F1) fire in neither, so their documented behaviour rests on reading the code plus
  one-off reproductions.
- **Nothing in the repository links to this directory yet.** `gpubench/verify.py`'s module
  docstring and the top-level `README.md` are both good places to point at `references/checks.md`,
  and neither does. Adding those pointers is a change to files this document does not own.
