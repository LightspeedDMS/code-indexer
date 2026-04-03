---
name: No fallbacks - one code path or fail
description: NEVER write fallback/alternative code paths. One path that works or fails loudly. No silent degradation.
type: feedback
---

No fallback code paths. No if/else branching between "try this, else try that" for the same operation. One code path that works for both modes (SQLite and PostgreSQL via the storage abstraction) or fails loudly.

**Why:** Silent fallbacks create an illusion of working software. Half-working code is worse than broken code because it hides failures, makes debugging miserable, leads to production incidents where behavior differs from expectations. The user expressed this extremely strongly -- fallbacks are unprofessional, unethical, and destroy engineers' quality of life.

**How to apply:** When implementing features that work across storage modes (SQLite/PostgreSQL), use the Protocol/BackendRegistry abstraction layer as ONE code path. Never branch on storage_mode in application code. If something can't work, raise an error -- don't silently degrade to an alternative. Remove dead code paths after refactoring.
