# Video craft notes

These are durable production lessons, independent of Chalk's implementation.

## Writing and editorial voice

Write as if you are thinking aloud at a whiteboard with a smart friend.

- Build an idea one piece at a time; introduce a definition when the viewer
  needs it.
- Use “you,” “we,” and “our” naturally, and weave concrete examples through
  abstractions.
- Connect each idea to the previous one, but let the narrative make the link
  rather than announcing it ("this is how this connects to what we just saw").
  Vary sentence length.
- Explain rather than sell. Precision is more accessible than vagueness, and a
  well-explained result does not need hype.
- Preserve the author's perspective and wording. Agents help with structure,
  accuracy, counterexamples, and revision; they do not sand away the voice.
- Fact-check definitions, theorem statements, attributions, dates, and any
  factual visual label. Record uncertainty instead of improvising.

Two registers, kept apart. Narration is what the viewer hears; director notes,
`outline.md`, and `feedback.md` are how we talk about the video. Craft shorthand
(beat, move, payoff, cue, land, earn) belongs in the notes, never in a spoken
line. In narration, name a step by its kind: a definition, a reduction, a
diagonal argument, a change of variables, a specialization, a construction.
Reserve "move," "trick," and "the game" for genuinely adversarial or game-tree
settings, where the metaphor is literally true.

Common AI tics to remove from narration. These are patterns; the listed terms
are only symptoms, so fix the pattern instead of swapping one flagged word for
another.

- Significance inflation: "profound," "pivotal," "crucial," "vital,"
  "groundbreaking," "remarkable," "elegant" asserted rather than shown; and the
  phrasal form, "stands as a testament to," "plays a vital role," "marks a
  turning point," "underscores the importance of." A good explanation carries
  the weight; the adjective does not.
- Filler openers: sentence-initial "Additionally," "Moreover," "Furthermore,"
  "Notably," "Importantly," "It is important to note that." Delete them and open
  on the content.
- Negation templates: "not just X, but Y," "it's not X, it's Y," "not only ...
  but also," and reflexive "X rather than Y." State the thing directly.
- Rule-of-three lists: three adjectives or clauses where one concrete example is
  stronger.
- Editorial participle tails: sentences that end in an "-ing" gloss:
  "..., highlighting its significance," "..., paving the way for," "...,
  reflecting a broader shift." End on the fact.
- Journey and dive clichés: "deep dive," "in the realm of," "navigating the
  complexities of," "unpack," "let's explore," "the world of." Just do the thing.
- Vague attribution: "studies show," "experts argue," "it is widely believed."
  Name the paper, person, and year, or cut the claim.
- Recap closings: "in summary," "in conclusion," "ultimately," a restatement of
  every point. End on one earned thought.

The Markdown has layout tells too: em-dash saturation, mechanical boldface,
Title Case headings, emoji dividers, and "**Term:** gloss" list padding. Plain
prose reads back better in a recording.

## Manim and layout

- Use `MathTex` for mathematics. Use `Text` for prose at comfortably readable
  sizes; below roughly `font_size=24`, inspect Pango/Cairo kerning carefully or use
  `MathTex(r"\text{...}")`.
- Test fonts at the actual delivery resolution. Prefer platform-stable fonts;
  the default face and Courier New are dependable on macOS.
- Cue a visual when its phrase is spoken. Chalk's `play_on` and `land_on`
  encode that relationship without copied timestamps.
- Remove or fade replaced elements. Residual labels create clutter, especially
  in narrow frames.
- Fractions and wide equations need explicit margin checks. For portrait work,
  stack rather than spread, enlarge text substantially, and increase vertical
  spacing.
- Network-layout coordinates are often 2D; Manim positions need a third
  coordinate.
- Manim's CLI resolution syntax is `W,H` (for example, `1080,1920` for a
  portrait frame). Chalk derives this from the selected profile.

## Audio, timing, and composition

- Record at a natural pace, then align visuals to the performance. Do not force
  a performance to match estimated timestamps.
- Whisper may hallucinate words in trailing silence; inspect the speech region
  and phrase alignment rather than trusting tail text blindly.
- The first model use includes a large download and is slower. Later exact
  model/audio/options combinations should reuse their transcript record.
- When audio outlasts animation, freeze the last frame with `tpad`. When video
  outlasts audio, pad audio rather than clipping the ending.
- Re-encode composed segment and full cuts; stream-copy concatenation can freeze
  or expose incompatible streams.
- Final loudness should use measured two-pass normalization targeting -14 LUFS
  and -1 dBTP.

## Review discipline

Review a segment immediately after its script, visual, or take changes. Inspect
the combined A/V, not just the animation. At final resolution, check typography,
frame edges, equation overlap, transition residue, timing, and the last frame.

Then watch the complete cut for intellectual continuity, pacing, loudness,
segment transitions, and whether the ending has been earned. Preserve every
unresolved observation in `feedback.md`.

When a lesson would help the next video—an animation pattern, a rendering
workaround, a writing smell, or a review check—add it here or upstream it to the
Chalk tool.
