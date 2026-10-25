# Packaging Triage

Language for comparing viewer-facing video packaging with assessable video content.

## Language

**Packaging**:
The displayed title and actual thumbnail together, including material words and salient depicted subjects.

**Material promise**:
A subject or claim prominently promised or implied by packaging, rather than incidental decoration or branding.

**Irrelevant**:
A material subject promised or prominently implied by the title or thumbnail is substantively off-topic relative to assessable video content; either source suffices even when the other is relevant.

**Clickbait**:
Subject-relevant packaging with a material claim that is misleading, contradicted, overstated or undelivered; a missing same-subject detail is not irrelevance.

**Sensational**:
Otherwise supported subject matter presented with emotionally loaded or exaggerated framing.

**Valid**:
Otherwise supported, fair, non-sensational packaging.

**Unavailable**:
An assessment status where inadequate or inaccessible evidence prevents classification, not a scored category.

## Browser presentation

A thumbnail receives a category frame only when a validated scored result has one
of the four estimates strictly above 50%. Exactly 50%, no majority, pending and
unavailable results stay uncoloured. This presentation rule does not change the
taxonomy or convert uncalibrated estimates into measured probabilities.

The frame is a persistent, pointer-transparent front-layer rim with an inward
spatial fade, not a temporal fade-away or an outline behind the thumbnail.
Activity distinguishes queued packages, native-dispatched local analysis attempts,
cached reuse, scored/unavailable outcomes and interrupted work of unknown inference
extent. Caption retrieval may fail before Gemma runs. Up to 100 validated input/result
summaries and Pause are retained only in browser-session RAM; raw evidence is not.
Up to three distinct packages run concurrently across all tabs, with 64 waiting;
each package's evidence/classification stages remain sequential. One private
runtime is configured for three inference lanes. Completed activity is evicted before active work.
Original regional/script English JSON3 candidates are tried manual-first without
translation or combining partial tracks. One 120-second retrieval deadline bounds
two metadata extractions and four caption downloads including retries; a Node/bundled
EJS refresh can recover some transient/access failures. Unavailable cache entries
expire after 60 seconds without an automatic retry loop; scored results retain the
one-hour cache. Missing, blocked/rate-limited and invalid/partial evidence remain distinct.
