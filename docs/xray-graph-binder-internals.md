# X-Ray Graph Binder Internals (Java/Kotlin)

Maintainer reference for the cross-file reference binder that
`analyze_graph` builds its graph from. **Nothing in this document is
required to write or interpret an `analyze_graph` evaluator** -- every
rule an agent needs for that lives in the tool's own doc
(`src/code_indexer/server/mcp/tool_docs/search/analyze_graph.md`), which
stands alone. This document exists for a maintainer auditing or extending
the binder itself, and captures the exact conditions behind claims the
tool doc states only as a short, actionable summary.

Split out by issue #1961 from `analyze_graph.md` (previously part of
its "Kotlin scope limits" prose and `edge_evidence`/`is_definitely_dead_code`
table cells) to keep the agent-facing doc within its context budget.

## Kotlin extraction scope and the tag-only guarantee

Kotlin is extracted at bind levels 0-2 (declarations, references, imports,
inheritance). It has no receiver-type substrate (its extractor never
populates `LocalIndex::typed_names`), so Kotlin hops never carry
`RECEIVER_TYPE_MATCH` from receiver narrowing -- but that costs EVIDENCE
QUALITY only, never an EDGE, for an ordinary qualified call:
`apply_receiver_type_narrowing` is permanently tag-only in this binder (it
may set `RECEIVER_TYPE_MATCH`, it never deletes a candidate), so a KOTLIN
instance-qualified call (`g.helper(x)`, a variable receiver) still binds by
name+arity exactly like a Kotlin type-qualified one (`Type.helper(x)`), and
a target reachable only through the instance form is not reported
definitely dead.

## Java qualified-call hard-narrowing

Deliberately different for JAVA only, gated on the CALLING file's language:
a call DEFINITELY qualified by a type reference (`Type.helper(x)`,
`Type::helper`) hard-narrows to exactly that type's own tagged
declarations, but ONLY when BOTH hold: the qualifier positively resolves to
a type declared somewhere in this repo, AND at least one candidate is
already confirmed to match it. Every other case -- the qualifier resolves
to no known in-repo type (an external/JDK receiver, or a same-named repo
declaration the extractor could not positively rule out) or resolves to a
type with zero matching candidates (an incomplete-extraction gap, not proof
of absence) -- is a deliberate NO-OP: the pool is left exactly as tag-only
narrowing would have left it, never emptied.

The qualifier-is-a-type conclusion itself guards against the shadowing
sources this extractor can see: a same-file local/parameter/field
(including an interface's implicit `public static final` constant fields)
and a single-member static import (`import static pkg.Holder.NAME;`)
anywhere in the file. The field check is REPO-WIDE, but only WITHIN the
files actually analysed this run -- an INHERITED field is invisible to it
whenever the declaring supertype sits outside the analysed set
(`include_patterns` scoping excluded it) or is declared by a KOTLIN file
(the Kotlin extractor never populates `typed_names` at all, so it records
no fields, ever, even for a Kotlin type that IS in the analysed set). Per
JLS 6.4.2 a field always wins over a same-named type reference at that
syntax position, so this gap is exactly why hard-narrowing carries two
further, WHOLE-FILE guards before it may fire at all: (1) the caller's file
must have NO static WILDCARD import (`import static pkg.Holder.*;` --
unlike a single-member import, it names no specific identifier, so nothing
can be positively matched against it; its mere presence disables
hard-narrowing for every call site in that file), and (2) **narrowing
applies only in files where NO type -- including a nested, local, or
anonymous class -- declares ANY `extends`/`implements` clause at all.**

Matching a supertype's NAME against a set of declared types is never
trustworthy evidence for this guard: a file can declare its own unrelated
type sharing the exact bare name of a call's real, externally-qualified
supertype, or the real supertype can be reached only through a sibling
nested class, an anonymous class body, or plain same-package resolution
with no import -- every one of these can make a name-based check pass
while the real supertype (the one actually declaring a shadowing field)
stays invisible to this binder. So the guard asks a purely syntactic
question instead: does this file record ANY supertype clause anywhere in
it, or any clause the extractor could not resolve to a name at all? If so,
the WHOLE file falls back to tag-only, not just the one affected call site
-- the clause's target NAME is irrelevant. A type with no supertypes at all
(an ordinary static facade, or any class implicitly extending only
`java.lang.Object` -- no `extends`/`implements` clause means no inheritance
edge is ever recorded) passes trivially and still hard-narrows; an enum or
record with no explicit `implements` clause also passes trivially (its
implicit `Enum`/`Record` supertype is never recorded as an edge and can
never carry an uppercase field), while one that DOES declare `implements`
disables the guard like any other type. None of this is an exhaustive
proof against every legal Java shape.

### Dotted (multi-level nested / fully-qualified) receivers (issue #1931)

A DOTTED (multi-level nested or fully-qualified) receiver
(`Outer.Inner.m()`, `com.example.Target.m()`) IS consulted, under the
identical guards above, via two structural resolution rules: a two-segment
chain (`Outer.Inner`) hard-narrows only when the repo recorded a REAL
nesting edge proving `Inner` is declared inside `Outer`'s own top-level
private-access domain; a fully-qualified chain of any length
(`com.example.Target`) hard-narrows only when a repo-declared type named
the final segment is declared in a file whose package exactly equals every
segment before it, joined by `.` (never a prefix/suffix match). A
fully-qualified NESTED spelling (`com.example.Outer.Inner.m()`, package +
nested type together) is NOT resolved by either rule and stays exactly
tag-only, unchanged from before #1931 -- the two-segment nested-type rule
requires exactly two segments, and the fully-qualified rule's own
package-equality check never matches (a nested type's recorded package is
its declaring FILE's package statement, e.g. `com.example`, never
`com.example.Outer`).

Either rule additionally requires EVERY segment (not merely the first) to
clear the same shadowing guards a bare qualifier's own identifier does: no
local/parameter binding anywhere in the file, no known field/interface-
constant name anywhere in the repo, no single-member static import naming
it. This is NOT a shortcut for "the chain is structurally always a field
access, never a type" -- Java lets a field and a nested type share one bare
name (JLS 6.3, separate namespaces), and per JLS 6.5.2 the field wins at
that syntax position whenever one exists, so a chain like `A.B.run()` where
`A` declares BOTH a field `B` and an unrelated nested class `B` genuinely
COULD be misread as the nested type by a guard that only inspected the
first segment -- checking every segment is what catches this. A
field-access chain (`obj.field.m()`, `Outer.FIELD.m()` where `FIELD` is a
static field) still stays untouched, but because the full-chain guard
positively detects the field, not because such a collision is structurally
impossible.

Two further guards close a real gap the per-segment field/local/import
checks above still cannot see on their own: a segment resolving to a repo
TYPE is checked against its FULL ancestor chain, transitively and
cycle-safely (walking every direct parent's own direct parent, and so on,
not merely the segment's own immediate superclass) -- "unresolved" here
means EITHER a syntactically-unparseable supertype clause anywhere in that
chain OR an ancestor, AT ANY DEPTH, whose bare name is not itself a
repo-declared type (an external/JDK/excluded-from-analysis class). Whenever
the chain is unresolved this way, that ancestor may itself have an
INHERITED field invisible to every check above no matter how many segments
are inspected -- so the whole chain bails (`class a extends ExternalBase
{}`, where `ExternalBase` is excluded from analysis but declares the
shadowing field, is the direct-parent shape; `class Outer extends
IndexedBase {}` / `class IndexedBase extends ExternalBase {}`, where
`Outer`'s own DIRECT parent `IndexedBase` IS indexed but `IndexedBase`'s OWN
parent `ExternalBase` is not, is the grandparent shape a direct-only check
alone would miss). The FQN rule separately bails outright whenever ANY
prefix segment is ALSO a known repo TYPE name, since a package name and a
real declared type's bare name occupy the identical lowercase-identifier
syntax space and Java never falls back to reading an accessible type name
as a package fragment.

Downstream tagging remains BARE-NAME-keyed like every other pass in this
binder: proving `com.example.Target` or a specific `Outer.Inner` resolves
correctly never disambiguates the resulting hard-narrow from an unrelated,
same-bare-name type elsewhere in the repo (a different `Target` in another
package, an `Inner` nested under a different outer) -- the real target is
never lost, but such a decoy can still be tagged alongside it, exactly as a
bare qualifier's own hard-narrow already tolerates for a bare-name
collision. Once a qualifier (bare or dotted) positively confirms a subset
this way, that subset is FINAL for this evidence tier: it is never subject
to a second round of import-context re-narrowing (`SAME_FILE`/
`SAME_PACKAGE`/import preference), which is reserved for genuinely
UNQUALIFIED references -- a same-bare-name decoy declared in the CALLER's
own file can no longer win over a real, fully-qualified, cross-package
target just because it happens to be closer by file/package proximity.
This has a real cost: a same-bare-name TYPE declared in an UNIMPORTED,
unrelated OTHER package can now also receive an edge that import-context
narrowing previously excluded on file/package-proximity grounds alone --
always ADDITIVE (the pass never removes an edge, only sometimes admits one
more decoy alongside a real one it was already keeping), so this never
turns a real edge into a false negative. A Java instance-qualified call
(`g.helper(x)`) is untouched by this and stays tag-only exactly as
described above.

## Kotlin operator convention extraction

`infix` calls (`a matches b`) and essentially every OPERATOR CONVENTION are
extracted: `a + b`/`a - b`/`a * b`/`a / b`/`a % b` (reaching
`plus`/`minus`/`times`/`div`/`rem`), `a < b`/`a <= b`/`a > b`/`a >= b`
(reaching `compareTo`), `a == b`/`a != b` (reaching `equals`), `m[k]`/
`m[k] = v` (reaching `get`/`set`, discriminated by assignment context),
`!f`/`-x`/`+x`/`x++`/`--x` (reaching `not`/`unaryPlus`/`unaryMinus`/`inc`/
`dec`), `a..b`/`a..<b` (reaching `rangeTo`/`rangeUntil`), `x in y`/
`x !in y` (both reaching `contains`), and `x += y`/`x -= y`/etc. on a
non-indexed target (reaching BOTH the `plusAssign`-family name AND the
plain `plus`-family name, since which one Kotlin actually picked cannot be
told apart without receiver-type evidence this extractor does not track)
all produce a real edge. Still NOT extracted: a COMPOUND assignment onto an
INDEXED target (`m[k] += v`, which still falls back to a plain `get`) and
the `invoke` convention (`f(x)` where `f` is a value of a type with
`operator fun invoke` -- indistinguishable from an ordinary call without
receiver-type information) -- a member reachable ONLY through one of these
two remaining forms can still be reported definitely dead while genuinely
called. Treat a Kotlin-only `definitely_dead` verdict on an `operator fun`
reachable only via `m[k] += v` or `invoke` as unproven.

## Kotlin single-line object-literal parse limitation

When an object-literal expression's own opening brace, a function member
inside it, and its closing brace all sit on a single source line (e.g.
`val o = object : Runnable { override fun run() {} }`), the Kotlin grammar
cannot parse it and loses the rest of that file's declarations. Such a file
is counted in `degradation.files_with_parse_errors` and the build reports
`fact_graph_complete: false`. Reformatting the SAME object literal across
multiple lines (its opening brace, the member, and its closing brace each
on their own line) parses correctly with full extraction -- no other
change needed.

## `is_definitely_dead_code`: edge sources and false-positive suppressions

Java extraction creates inbound edges for direct calls, method references,
`new` expressions, explicit `this(...)`/`super(...)` constructor
invocations, `Type::new`, and annotation usages (an edge to the annotation
type's declaration); it preserves plausible overload and varargs targets.
`super` calls bind only to a KNOWN, recorded superclass edge; the
conservative "no evidence" fallback applies only when a class has NEITHER
an `extends` NOR an `implements` clause (e.g. implicit `java.lang.Object`,
never tracked) -- a class with no `extends` but a real `implements` clause
still narrows against its recorded interfaces. With neither clause,
`super.m()` falls into the same "no supertype evidence" case as a genuine
extraction gap, so it can still self-loop when the enclosing type's own
method is the sole matching candidate. Java-private candidates from a
different known top-level type are excluded.

Two Java-specific false-positive classes are addressed. First: a
`private Foo() {}` no-arg constructor that is the ONLY constructor its
class declares -- the standard non-instantiable-utility-class idiom, where
"unreferenced" is intentional, not dead -- is excluded from the
`Some(true)` verdict via a SEPARATE per-symbol fact the predicate
additionally consults, never by changing the constructor's own declared
visibility: `g.visibility_of()` still reports `Private` for it exactly as
extracted, even though `is_definitely_dead_code` reports `None` instead of
`Some(true)`. Second: a method named by a JUnit5 `@MethodSource` string
literal (bare, array, `value = "..."`, the empty-value same-name default,
or a `Class#method` form self-qualified to the annotation's own enclosing
class) is marked as referenced evidence at extraction time, so a
reflection-invoked parameterized-test data provider is not reported dead
either -- this only suppresses the provider's OWN `is_definitely_dead_code`
verdict; it is NOT a `Reference`/CSR edge, so it never appears in
`callers_of`/`callees_of` or any reachability result. An explicit but
unresolvable argument (e.g. naming a different class) leaves the target
unreferenced rather than guessing.

## `signature_for`: varargs rendering detail

For a METHOD, `signature_for` renders `Owner.name(ParamType, ...)` --
declaring type, method name, and bare (generic-stripped, never
fully-qualified) parameter type names, e.g.
`TimeUtil.parse(XMLGregorianCalendar)`. A VARARGS parameter renders with
its REAL per-language spelling, never a bare type name indistinguishable
from a genuine one-arg overload: Java spells it with a trailing ellipsis
(`Reader.consumeToAny(char...)`, the real `char... chars` source syntax) --
ALWAYS the last parameter (JLS 8.4.1 guarantees this) -- while Kotlin
spells it with a leading `vararg` keyword (`Api.logAll(vararg Int)`, the
real `vararg xs: Int` source syntax) at whatever position it ACTUALLY
occupies: unlike Java, Kotlin allows exactly one `vararg` parameter at ANY
position (`Api.mid(vararg Int, String)` for `fun mid(vararg xs: Int, tail:
String)`, where every parameter after it must be passed by name at the call
site) -- never assume it is last for a Kotlin signature. Falls back to
`Owner.name(N params)` when the extractor captured fewer parameter types
than the recorded arity -- a partial list is NEVER presented as complete,
so this fallback text never shows a varargs marker either -- and to
`name(...)` with no prefix when the declaring type is unknown (a Kotlin
top-level function). Non-method declarations keep their extractor-supplied
text unchanged. An anonymous or enum-constant-body class's `Owner` renders
as `Enclosing$<anon@L<line>:<file_id>:<byte>>` -- the immediately enclosing
type's real bare name plus the anonymous body's own real source line up
front (human-chaseable), with `file_id`/`byte` kept after that prefix only
to guarantee global uniqueness across the whole analysed set (never
parse/rely on their exact values). No annotations, no return type, no
modifiers.

## `RECEIVER_TYPE_MISMATCH`: exact preconditions

`RECEIVER_TYPE_MISMATCH` is set ONLY when ALL FOUR of the following hold
(same grouping and count as `graph::reasons::RECEIVER_TYPE_MISMATCH`'s own
doc comment, the source of truth):

1. The call's receiver has a POSITIVELY known declared type that is
   CLOSED-WORLD (`String`, a primitive, a boxed wrapper, or any array type)
   whose bare simple name is NOT ALSO a repo-declared type, an explicitly
   imported type (ordinary OR single-member static), or a known generic
   type parameter (a repo can legally declare its own class named
   `String`, shadowing `java.lang.String` for code in that package).
2. The receiver binding is a genuine method/constructor PARAMETER, never a
   block-scoped local variable or a field (a field can be misresolved to
   an unrelated, same-method local's declared type under this binder's own
   per-method-not-per-block scoping limits).
3. The parameter's declared type was written UNQUALIFIED or qualified
   exactly as `java.lang.*` (this binder's type model only ever records a
   bare simple name, so `com.lib.String s` and `String s` are
   indistinguishable, even though `com.lib.String` is a different, unproven
   type that may legally have a repo-declared subtype).
4. EVERY type declared in the call site's own FILE has fully repo-resolved
   supertype evidence, on ANY nesting level (a type extending an
   external/unindexed supertype may have a nested type privately shadowing
   a closed-world name this binder cannot see into) -- judged by SIMPLE
   NAME only, so a repo-declared type sharing the same bare name as the
   real external supertype can make it look "resolved" when it is not
   actually the same type.

Under all four conditions its PRESENCE is real evidence the candidate is
unrelated, even though (like every bit here) it never deletes the
candidate itself. Pair it with the `*_filtered` primitives (e.g.
`forbidden_bits: RECEIVER_TYPE_MISMATCH`) to drop these fabricated edges.

## `OVERLOAD_ARG_TYPE_MATCH`: closed-world matching rules

`OVERLOAD_ARG_TYPE_MATCH` means NO ARGUMENT WITH A KNOWN DECLARED TYPE is
provably incompatible with this candidate's declared parameter types,
checked per position for every argument this binder can resolve a type for
at all (a literal, a cast, `new T(...)`, `this`, or a same-file
local/parameter/field with a real declared-type record).

For a NAMED CLASS/INTERFACE argument type, this bit NEVER removes a
candidate from the pool -- named-type argument evidence is permanently
TAG-ONLY, exactly like `RECEIVER_TYPE_MATCH`, and for the same reason: this
binder cannot generally prove two class/interface NAMES are unrelated (a
repo type can share its bare name with an unrelated external type, hiding
the external type's real relationships), so incompatibility is only ever
proven via a small CLOSED-WORLD rule -- Java primitive/String/boxed-wrapper
types only (exact match, `Object`, primitive/boxing widening including
`char`, `String -> CharSequence`, arrays by closed-world element type,
varargs `T...` accepting both `T` and `T[]`). For any two NAMED
CLASS/INTERFACE types outside that closed-world rule, this bit is set
whenever BOTH simply have a known declared type, regardless of whether they
are actually related. It is a "not provably incompatible" signal, never a
"confirmed match" one: an argument whose type this binder cannot resolve at
all (a chained call, a field access through an arbitrary receiver, a
lambda) is always treated the same way, so the bit routinely appears on a
candidate this binder simply had no evidence against; several same-owner
overloads (e.g. differing only by autoboxing, or all class/interface-typed)
are routinely tagged TOGETHER.

For a LITERAL argument (an int/string/boolean/char literal, whose type the
binder knows directly and unambiguously, never via a name that could
collide with an unrelated type), the same closed-world rule is not merely
tag-only -- a literal PROVABLY incompatible with a candidate's declared
parameter type removes that candidate from the pool outright. This check is
gated on the candidate's parameter types having actually been RECORDED by
the extractor at all -- an unresolved/unrecorded parameter is treated as
unknown (no filtering), never as a mismatch -- and it applies to JAVA
callees ONLY: a non-Java (Kotlin) callee is never subject to this
argument-type check regardless of receiver evidence, consistent with
Kotlin's broader lack of receiver-type substrate documented above.

A hop carrying only `ARITY_MATCH | OVERLOAD_ARG_TYPE_MATCH` (no
receiver/context bits at all) matched on argument count and an
absence-of-proven-mismatch alone -- for an unqualified call this still says
nothing about whether the true target is even in this repo, and does not by
itself distinguish the real overload from a same-arity sibling.
