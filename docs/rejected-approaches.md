# Rejected approaches

Approaches that were built, measured, and rejected. Kept because the evidence
is the useful part: without it these look like obvious things to try, and
someone will try them again.

---

## Component-scoped retrieval

**Idea.** Constrain nearest-neighbour retrieval to complaints sharing at least
one component with the seed, at full subcategory granularity
(`components && seed.components`). Motivated by a seed whose neighbours were
all genuine Tesla electronics failures but too general to be one defect
pattern — screen freeze, cabin heater, AP4 computer — matching at roughly
"expensive Tesla electronics broke".

**Tested.** Query-side only, no re-embedding, on the boilerplate-stripped
vectors, over a shared pool of 3,986 complaints. Five seeds, same similarity
bands, constrained vs unconstrained.

**Result — it did not fix the case it was designed for.** On the motivating
seed it removed the AP4 computer complaint, but *kept* the cabin heater
(tagged `ELECTRICAL SYSTEM`, which overlaps), and removed the single best
match in the pool: a Model 3 screen-freeze complaint at similarity 0.8594
describing the seed's exact failure. That complaint is filed under
`UNKNOWN OR OTHER`, so it shares no component with anything specific.

| Seed | unconstrained ≥0.74 | scoped ≥0.74 | lost |
|------|--------------------:|-------------:|-----:|
| screen freeze     | 378 | 114 | 70% |
| door opened       | 349 |  76 | 78% |
| phantom braking A | 319 | 296 |  7% |
| black screen      |   7 |   4 | 43% |
| phantom braking B | 579 | 523 | 10% |

**Why rejected.**

1. **`UNKNOWN OR OTHER` is a hole in the index.** 2,955 complaints (14% of the
   corpus) carry it — the fifth most common component value. Under this
   constraint such a complaint can only ever match another `UNKNOWN OR OTHER`
   complaint, whatever its narrative says. That single fact cost the
   motivating seed its best neighbour.
2. **It reintroduces the dependency the project exists to escape.** The brief's
   premise is that NHTSA's component categories are too coarse to separate
   failures. Scoping retrieval by component puts that coarseness back into the
   one step meant to get past it.
3. **It costs most where recall is scarcest.** The rare-failure seed dropped
   from 7 neighbours to 4. A system for surfacing emerging patterns cannot
   afford to thin out the sparse cases.

**What it did do, and did not.** It cut long-tail volume substantially on
diffuse seeds (378 → 114) without changing what sat at the top, and it was
harmless to cross-manufacturer grouping: the phantom-braking seed kept all 8
non-Tesla neighbours ≥0.74 at identical scores, including a Honda Civic at
0.8586 among Teslas. So the failure was not that it broke cross-make matching
— it simply did not buy precision where precision was needed.

**Status.** Rejected as a hard retrieval constraint. Possibly still viable as a
post-filter or tie-breaker on an already-retrieved set, where the
`UNKNOWN OR OTHER` hole costs recall rather than correctness — untested.

---

## A single global similarity threshold

**Idea.** Pick one cosine-similarity cutoff and take every neighbour above it,
per the brief's original plan to sweep a threshold and show the coherence
curve.

**Tested.** Five seeds, boilerplate-stripped vectors, counted neighbours at
0.74 / 0.78 / 0.82 / 0.86 / 0.90.

**Result — neighbour density varies by two orders of magnitude across seeds
at the same cutoff.**

| Seed | ≥0.74 | ≥0.78 | ≥0.82 | ≥0.86 |
|------|------:|------:|------:|------:|
| screen freeze      | 378 | 82  | 9   | 0  |
| door opened        | 349 | 123 | 18  | 0  |
| phantom braking A  | 319 | 65  | 3   | 0  |
| black screen (F-150)| 9  | 2   | 0   | 0  |
| phantom braking B  | 579 | 404 | 134 | 17 |

**Why rejected.** No single cutoff means the same thing for both a common
failure (phantom braking B: 404 neighbours at 0.78) and a rare one
(black screen: 2 neighbours at the same cutoff). A threshold loose enough to
give the rare seed a usable neighbourhood floods the common seed with
hundreds of candidates; a threshold tight enough to keep the common seed's
top clean starves the rare seed to nothing. The single number was being
asked to do two incompatible jobs — noise control on dense clusters and
recall on sparse ones — depending on which seed it landed on.

**Status.** Rejected as the sole retrieval mechanism. A loose floor (0.78)
survives into the settled design, but not as the precision mechanism —
that work moved to pattern formation.

---

## A single global top-k

**Idea.** Take the top k nearest neighbours by rank instead of everything
above a threshold, motivated by a seed where the two genuinely correct
matches ranked above a spurious one (0.8594, 0.8368 correct; 0.8273 the
spurious cabin-heater match) — so a small k looked like it would cut the
noise for free.

**Tested.** k ∈ {5, 10, 20}, floor 0.78, same five seeds, full neighbour
lists read for where coherence broke down by rank.

**Result — the premise didn't hold, and no single k works either.**

The motivating seed's spurious match was at rank 4, inside k=5 — top-5 does
not exclude it. Only k=3 does, and k=3 also discards rank-1 and rank-3,
which were correct. Coherence did not degrade monotonically with rank on
either affected seed: genuine matches for the "screen freeze" seed continued
to appear at ranks 5, 9, 10, and 15, interleaved with wrong matches at 6, 8,
14 — no cut point separates them cleanly.

The break rank varied from 3 (tightest read) to beyond 20 (two seeds never
broke down inside the top 20) across five seeds — the same order-of-magnitude
spread as the threshold experiment, on a different axis.

**One result argues specifically against small k.** A phantom-braking seed's
cross-manufacturer matches — the exact signal this project exists to
surface — sat at ranks 12 (Chevrolet Silverado), 17 (Jeep Grand Cherokee),
and 20 (Honda Civic). `k=10` would have excluded all three; only `k=20`
caught the Honda, and only by one rank. A tight k systematically favors
same-make matches, which crowd the top of any seed's neighbour list purely
by shared vocabulary, over the cross-make matches that would actually
demonstrate a pattern generalizes beyond one manufacturer.

**Status.** Rejected as the sole retrieval mechanism, for the same structural
reason as the threshold: it asks one number to do noise control and recall
at once, and those pull in opposite directions per seed. A loose cap (25)
survives into the settled design as a ceiling on retrieval cost, not as the
thing doing precision work.
