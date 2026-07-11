# Video craft notes

These are durable production lessons, independent of Chalk's implementation.

## Writing and editorial voice

Write as if you are thinking aloud at a whiteboard with a smart friend.

- Build an idea one piece at a time; introduce a definition when the viewer
  needs it.
- Use “you,” “we,” and “our” naturally, and weave concrete examples through
  abstractions.
- Connect each move to the previous one. Vary sentence length.
- Explain rather than sell. Precision is more accessible than vagueness, and a
  well-explained result does not need hype.
- Preserve the author's perspective and wording. Agents help with structure,
  accuracy, counterexamples, and revision; they do not sand away the voice.
- Fact-check definitions, theorem statements, attributions, dates, and any
  factual visual label. Record uncertainty instead of improvising.

Common AI tics to remove: significance inflation (“profound,” “pivotal,”
“crucial”), repeated “not just X—but Y” constructions, rule-of-three lists,
paired rhetorical questions, em-dash saturation, superficial participle tails,
and recap closings. End on one earned thought.

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
