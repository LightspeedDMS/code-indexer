# Query-Embedding Cache: Empirical Study and Alternatives Analysis

This is the durable record of the experiment that determined how the
query-embedding cache normalizes its keys, and why a set of more aggressive
alternatives was rejected. It promotes the working notes from
`.analysis/embedding-cache-experiment/REPORT.md` into a permanent design record.

All numbers below are quoted from that report's measured results (artifacts:
`run_experiment.py`, `supplementary.py`, `evolution_hnsw_experiment.py`,
`anchor_reorder.py`, `anchor_depth.py`, `reorder_realism.py`,
`semantic_drift.py`, `spotcheck.py`). Where a figure is a sampling-bounded
estimate, that is noted.

---

## 1. Question

Can normalizing query strings let a string-keyed embedding cache skip the live
embedding round-trip (200ms to 3s) and jump straight to HNSW search, for CIDX's
two core query models: VoyageAI voyage-code-3 (1024-dim) and Cohere embed-v4.0
(1536-dim)?

The determinant of cache value is the query-repetition rate, not normalization
cleverness. The study set out to find the most aggressive key normalization that
is still result-faithful.

---

## 2. Method

- 50 intents, 5 phrasings each (imperative / question / keyword noun-phrase /
  reordered-synonym / verbose-with-filler) = 250 phrasings.
- 8 normalization strategies, incrementally aggressive: S0 raw; S1
  lowercase+whitespace; S2 +strip punctuation; S3 +alphabetical token sort; S4
  +stopword strip; S5 +Porter stem; S6 +token dedup; S7 +aggressive
  search-filler strip.
- Embeddings produced through CIDX's OWN provider clients with
  `embedding_purpose="query"` (Voyage sends no input_type; Cohere uses
  `input_type="search_query"`). Both providers return L2-normalized vectors, so
  cosine equals dot product.
- Two cache schemes: Scheme A caches `emb(normalize(q))` and serves the
  normalized string's vector; Scheme B caches `emb(q_first)` under key
  `normalize(q)` and serves the first real sibling's vector (never embeds a
  mangled string).
- A decisive follow-up replaced the cosine proxy with a real HNSW top-k overlap
  test against the live voyage-code-3 evolution index (about 20,135 vectors).

---

## 3. Key findings (with the numbers)

### 3.1 Exact keys are 100% result-faithful

Strategy S0 (raw, no normalization): served cosine mean 1.000, p5 1.000, 100% of
served vectors at cosine >= 0.95, for BOTH providers. In the real-HNSW test,
exact match gives chunk-overlap 1.000, file-overlap 1.000, top-1 same 100%,
RBO 1.000. An identical query string produces an identical embedding produces
identical results. Exact-match caching is the only normalization that preserves
results perfectly.

### 3.2 Lowercasing is embedding-lossless but breaks CamelCase identifier matches

In cosine terms lowercase+whitespace+punctuation normalization is nearly lossless
(Voyage 0.998 served cosine, Cohere exactly 1.000; Cohere normalizes
case/punctuation internally, Voyage is near-invariant). Cosine OVERSTATED
stability. On the real voyage-code-3 evolution index, lowercase-only still
changed the top-10 substantially:

| metric (top-10 vs RAW) | lowercase-only |
|------------------------|----------------|
| chunk-overlap@10 mean | 0.774 |
| top-1 same chunk | 66% |
| identical ordered top-10 | 0% |

Top-1 flips about 34% of the time under lowercase-only despite ~0.998 cosine, and
NO query kept an identical ordered top-10. The mechanism that makes lowercasing
unsafe for code is genuine signal loss: lowercasing breaks matches to CamelCase
identifiers. The report's example: "Background Data Synchronization" RAW returns
the BackgroundProcessor / BackgroundPayloadWorker / BackgroundProcessService
cluster; the lowercased "background ..." loses that cluster entirely (9 of 10
results change) and returns unrelated UI/widget files. For code search, case
carries identifier signal. Lowercasing is therefore BANNED for the key.

### 3.3 Reordering is non-consequential lateral churn but still flips top-1 about half the time

Alphabetical sort / arbitrary reorder damages the vector more than case does. On
the real HNSW index:

| metric (top-10 vs RAW) | reorder-only | lowercase + reorder |
|------------------------|--------------|---------------------|
| chunk-overlap@10 mean | 0.689 | 0.642 |
| top-1 same chunk | 49% | 41% |

A separate test (`semantic_drift.py`) established that the reorder churn is NOISE,
not semantic drift: front-loading a query term raised the results' affinity to
that term only at chance (diagonal-dominance 8/27 = 30%, exactly the 1/n random
null; mean diagonal-minus-column effect +0.0014, std 0.0065 = zero within noise).
Reordering moves the query embedding in an essentially random direction; the
top-10 reshuffle is arbitrary flipping of near-tied chunks on a dense index,
lateral churn among equally-relevant candidates, not a meaningful re-emphasis.

The churn is "different but not worse" but it still flips the specific top-1
result roughly half the time, so it is NOT result-faithful for a cache. The
report also disproved "small natural reorders are safe": even a single adjacent
word swap gave chunk-overlap 0.706 and top-1 same 50% (barely better than a
random shuffle's 0.689 / 49%), and the gradient across reorder magnitude is
shallow with top-1 essentially flat at ~47-50% at every distance.

### 3.4 Anchor-first-two is the recommended dial midpoint at about 77% top-1

The anchoring-depth test (`anchor_depth.py`, 15 grounded 5-6 token DMS queries,
original case, vs RAW) measured fixing the first k tokens and reordering the tail:

| anchor depth | n | chunk-overlap | file-overlap | top-1 same | RBO |
|--------------|---|---------------|--------------|-----------|-----|
| k=0 full shuffle | 60 | 0.675 | 0.747 | 57% | 0.622 |
| k=1 anchor-first | 60 | 0.715 | 0.779 | 70% | 0.685 |
| k=2 anchor-first-two | 60 | 0.748 | 0.789 | 77% | 0.736 |
| k=3 anchor-first-three | 36 | 0.811 | 0.870 | 75% | 0.776 |
| exact match | -- | 1.000 | 1.000 | 100% | 1.000 |

Anchoring depth is a continuous fidelity-vs-collapse dial: each anchored prefix
token reduces drift but fewer tail reorderings collapse to one key (fewer hits);
as k approaches the token count it converges to exact-match in both fidelity and
hit rate. anchor-first-two is the strong midpoint: top-1 77%, overlap 0.748, RBO
0.736, beating lowercase-only's 66% top-1 on these longer queries. The k=3 top-1
dip (75% vs 77%) is within sampling noise (n=36; 5-token queries yield only one
k=3 variant). This is why the cache default anchor depth is 2.

The earlier anchor-first proposal test (`anchor_reorder.py`) isolated WHY the
lead token matters. anchor-first (depth 1) vs an anchor-LAST control: set overlap
is essentially equal (0.702 vs 0.696), but top-1 differs sharply (60% vs 46%) and
RBO (0.664 vs 0.619). Set membership is governed by total shuffle distance and
fixing ANY one token reduces it equally; but position 0 is specifically special
for the #1 result. The lead token disproportionately determines the top hit.

### 3.5 Cosine overstates stability versus real HNSW top-k

The single most important methodological finding: cosine is a poor proxy for
HNSW top-k stability on a large dense code index. Lowercasing is ~0.998 cosine on
Voyage yet flips top-1 34% of the time on the real index. Any cache fidelity
claim must be validated against real top-k overlap, not cosine. The two
mechanisms behind the gap are benign tail reshuffle (near-tied results swap ranks
3-10 while top-1/2 and the result SET hold) and genuine signal loss (the
CamelCase case above).

### 3.6 Lexical normalization does not collapse semantic paraphrases

Even at maximum aggressiveness (S7) only about 0.22 of 5 phrasings merged per
cluster (about 4% extra hits). Different phrasings use different content words;
case/order/morphology normalization cannot merge "login" / "authentication" /
"credentials" / "sign in". The aggressive strategies that DO create hits create
low-fidelity hits: S5/S6/S7 served cosine drops to ~0.78-0.85 (Voyage) and
~0.74-0.83 (Cohere). Scheme B strictly dominates Scheme A: under Scheme B the key
normalization only decides equivalence and the served vector is always a real
query embedding.

### 3.7 Voyage degrades more gracefully than Cohere

On every axis (paraphrase baseline, order-invariance, loss under sort/stem)
Voyage tolerates normalization better. Baseline intra-cluster paraphrase cosine
mean: Voyage 0.737 vs Cohere 0.535. If a normalization-keyed cache is pursued,
Voyage is the safer provider; this is reflected in the per-provider anchor and
mode knobs.

---

## 4. Rejected alternatives

Each of the following was considered and rejected on the evidence above.

### 4.1 Cosine-threshold (similarity) cache: REJECTED

A cache keyed on "is this query within cosine T of a cached query" was rejected
for two reasons. First, it does not save embedding latency at all: to compare by
cosine you must first embed the incoming query, which is the exact cost the cache
exists to avoid. Second, it is exposed to cross-intent near-collisions that a
string key is immune to. The report found pairs of DIFFERENT intents with high
cosine, e.g. 0.814 between background_jobs "async task scheduler" and
scheduled_tasks "periodic task scheduling", and 0.789, 0.771, 0.765 for other
distinct-intent pairs (Voyage). The closest pair of DIFFERENT intents (0.814)
scored higher than the least-similar pair of SAME-intent phrasings (0.500): the
intent clusters overlap in cosine space, so a threshold cache would conflate
distinct intents. A string key (different content words to different keys) cannot.

### 4.2 Alphabetical full-token sort for the key: REJECTED

Full sort is the single most damaging "normalization" relative to the hit rate it
buys. Reorder-only top-1 on the real index is 49% (chunk-overlap 0.689); a single
adjacent swap is already 50% / 0.706. It flips the top result about half the time
for almost no extra hits (sort buys only the S3 collapse, which on the cosine
table is still 5.00 distinct keys per cluster, 0% hit rate). Anchor-first-two
strictly dominates full sort on both axes the user cares about. Full sort is
rejected; the tail-only sort in anchor-token normalization is the retained,
bounded form.

### 4.3 Stopword strip / Porter stem / aggressive filler strip: REJECTED

S4/S5/S7 materially damage the embedding (Voyage to ~0.86 p5 under sort, lower
under stem; Cohere to ~0.60 p5) for almost no hit-rate gain (about 1-2% each),
and the hits they create are low-fidelity (~0.78-0.85 served cosine), i.e. they
merge queries the model considers different. Each buys ~1-2% hit rate at a 7-17%
fidelity cost. Rejected.

### 4.4 Lowercasing the key: REJECTED (and a hard invariant)

See 3.2. Lowercasing is gentle on cosine but carries a real CamelCase
identifier-signal failure mode on a code index, flipping top-1 about 34% of the
time and losing entire relevant identifier clusters. For a code-search product
this is the worst failure mode. NEVER lowercase the key is a hard invariant of
the implemented cache.

### 4.5 A per-node RAM layer: DEFERRED (not built)

A RAM layer was deliberately NOT built. The real workload is about 500 semantic
searches/day with a 30/sec ceiling, so a synchronous per-query DB round-trip is
trivial and a RAM layer would add cache-coherence complexity across cluster nodes
for no measurable latency benefit at this QPS. A RAM layer remains a clean,
purely additive optimization that can be added later if QPS ever grows. Because
there is no RAM layer, the shared DB count cap is the single true cluster-wide
cap.

---

## 5. Behavioral conclusion and what shipped

Claude's strategic ordering matters for caching NOT because small reorders are
safe (they are not) but because Claude tends to NOT reorder at all: it reproduces
the same canonical string, and its real variation is mostly different WORDS
(synonyms, question vs noun-phrase form), not reorderings of the same words. That
consistency is the argument for exact-match caching (it lands the hit and is 100%
result-faithful) and an argument against sort/normalize-the-key caching (unsafe
regardless, and unnecessary because same-word reorderings are rare in practice).

What shipped (see `docs/query-embedding-cache.md`):

- Anchor-token key normalization with a default depth of 2 (anchor-first-two):
  first 2 tokens kept in order, tail sorted, case PRESERVED. At depth >= the
  token count this is exact-match.
- Case is never lowercased.
- Scheme B in effect: the stored vector is always a real query embedding; the key
  only decides equivalence.
- Per-provider anchor depth and mode knobs (Voyage degrades more gracefully).
- A shadow mode that measures would-serve hit rate and `cos(cached, live)`
  without changing any served result, plus a sampled deep-fidelity audit that
  checks real HNSW top-10 overlap and top-1 match (the validation the report
  recommended as the next step before shipping).

---

## 6. Caveats from the study

- The 5 phrasings per intent were deliberately diverse. Real Claude-generated
  variation is often lower-diversity (same content words, reordered/filler),
  where sort/stopword normalization would collapse more; but the word-order
  result shows the served vector would still be ~6-16% off cosine even then.
- The anchor-depth numbers come from small grounded query samples (n as noted per
  row, some rows n=36/60). They establish the monotonic dial and the
  anchor-first-two midpoint, not population-precise rates.
- The decisive real-HNSW results are Voyage-only (the evolution index is
  voyage-code-3). Cohere behaviour was measured in cosine terms only; it degrades
  more, not less, which is why the cache exposes per-provider knobs.
