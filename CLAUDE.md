# AutoDoc — working agreement

Read `HANDOFF.md` before changing code: invariants and gotchas, ~350 lines.
`README.md` is the map and setup guide. `pyside6-ui-style-guide.md` is a binding
contract for UI work; its §11 checklist defines "done".

## Comments

Comments are 1–3 sentences that clarify genuinely unclear design. Not essays.
A 4,000-line comment surplus was removed from this repo on 2026-08-18; do not
rebuild it.

- **Module docstring: one line.** Two or three only if the file has a real
  contract with another file that a reader must know about.
- **Function docstring: only where the signature doesn't already say it.** One or
  two lines. A docstring that restates the name and parameters is worse than
  none.
- **Inline comments earn their place three ways only:** explaining unidiomatic
  code, marking a bug fix that looks wrong without the note, or flagging
  incomplete work. Two sentences maximum.
- **Delete on sight:** dated changelog prose ("added 2026-08-18", "SUPERSEDED"),
  alternatives-considered narrative, restatements of the next line, arguments
  with a hypothetical future maintainer, war stories about how a bug was found.
- **If a comment needs to be long, the code is unclear — fix the code.** A named
  helper or a better constant name beats a paragraph. This is the rule that
  matters; the others follow from it.
- **Rationale does not get relocated.** Moving an essay from a module docstring
  into `HANDOFF.md` is not a fix. Keep a fact only if losing it would cause a
  regression; delete the argument for it.

Reference: Stack Overflow's *Best practices for writing code comments* — the
nine rules this section is derived from.

## UI text

The same standard. Prefer a self-explanatory label over a caption explaining it
("Manual screenshot" → "Screenshot without clicking" needed no help text at
all). Keep captions under ~60 characters. Assume the reader understands ordinary
controls; write help text only where the consequence of a setting is invisible.

## Verifying changes

- No automated test suite. `python main.py` from source is the fastest real
  check; `generation/` and `publish/` are the parts worth unit-testing.
- Qt **is** testable headlessly: `QT_QPA_PLATFORM=offscreen` plus
  `PySide6.QtTest.QTest`. It has caught real coordinate and focus bugs that
  static review missed. Widget `geometry()` assertions can even catch overlap.
- For a comment-only pass, prove it with an AST diff that strips docstrings.
  That is what made a 44-file sweep safe.
- Static checks cannot see the UI defects that reach users. Say plainly when a
  visual pass is still needed, and what to look at.

## Environment notes

- Do not run `git` against this repo from a Linux sandbox — it leaves a
  `.git/index.lock` the mount cannot unlink, which blocks commits from Windows.
- Screenshots and recorded sessions in `sessions/` may contain student data.
  Never commit them.
