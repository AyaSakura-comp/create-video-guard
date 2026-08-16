# create-video-guard

Pi extension that enforces a SQLite-backed visual-review state machine for the
`create-video` workflow.

## Stages

`vague brief → treatment → character_sheet → cut_plan → storyboards → clips → final`

The user's original wording is preserved while the agent fills in the production details:
explicit requirements, attributed assumptions, creative choices, treatment, exact-duration
Shot Manifest, continuity bible, and audio plan. This versioned treatment must reach
`treatment_approved` before a character sheet can be submitted. Once the workflow leaves `brief`,
the treatment cannot be redefined underneath dependent cut plans, artifacts, or reviews.

For `project_type=mv`, `video_define_brief` additionally requires an existing MP3/WAV source,
hashes it, and requires each 2–5.2 second shot to declare its source-audio in-point and a
`vocal_performance.mode` of `none` or `singing`. A singing shot must lock `subject_id`, stable
`speaker_id`, source language, and exact untranslated lyrics for H3's `<d>` block. The MV audio
plan must mark generated audio as `reference_only` and set
`final_audio_policy=remux_original_source`.

After storyboard approval, the shell gate rejects first-frame FL2VA and requires the local R2V
route: `create_video.sh --mv --reference-image ... --reference-audio ...`. Singing guidance
returns structured per-shot prompt requirements that assign `<Picture 1>` to identity/composition,
`<Audio 1>` to vocal timing, and each visible singer to its own stable speaker such as `(S1)`.
Raw lyric/language text is kept out of executable shell templates; exact UTF-8 prompts are passed
through a base64 token. Clip review adds project-aware checks for audio-reference
timing; singing shots additionally require exact visible lyrics, aligned vocal onset and phrase
end, M/B/P closures, mouth closure during rests, and an unobstructed mouth. Final MV review cannot
pass until the original source song is remuxed and aligned to the locked timeline. The vendored
official ComfyUI template is
`~/.pi/agent/skills/create-video/workflows/video_minimax_h3_r2v.json`; the executable API graph is
implemented by `~/src/ComfyUI/scripts/minimax_h3_ref_generate.py`.

After character-sheet approval, `video_define_cut_plan` creates the authoritative editorial plan
before any storyboard can be generated. An editorial cut lasts **1–15 seconds** and is chosen from
story function, action density, camera changes, dialogue/lyric phrases, and scene changes. Every cut
is persisted twice: as versioned canonical JSON with SHA256 and as normalized `cut_plan_items` rows
whose independent columns contain duration (including canonical exact-decimal text), scene id,
`start_frame_json`, `action_json`, and `generation_segments_json`. Production and cut timing is
parsed with `Decimal`, persisted canonically, and exposed with `*_exact` status fields.

Each cut's `start_frame` must completely describe the scene, every character, objects, pose,
expression, composition, initial camera, and lighting. Its `action` must describe camera movement,
scene changes, ordered character and facial actions, complete body motion, temporal progression,
exact end state, and sound. The cut plan receives its own structured boolean review before it can
unlock storyboards.

Editorial cuts are distinct from local H3 generation segments. A cut may last up to 15 seconds,
but each nested local segment remains at most 5.2 seconds. Segment zero starts from the cut's
storyboard; later segments remain inside the same editorial cut and use the previous generated
segment's actual last frame. Segment offsets must be contiguous and durations must sum exactly to
the parent cut; cut durations must sum exactly to the production target.

After cut-plan approval, storyboard guidance queries each cut's stored `start_frame` and requests
one first-frame image per editorial cut in cut-plan order. Submission requires exactly one image
for every cut and persists each storyboard's `artifact_key=cut_id`; missing, extra, repeated-path,
or duplicate-SHA images cannot reach review. After storyboard approval, video guidance queries the
stored `action` plus its ordered generation-segment slices. Shell gates reject storyboard creation
before `cut_plan_approved` and reject H3 generation before `storyboards_approved`.

## Deterministic small-model guidance

Every successful workflow tool result includes `workflow_guidance`: exactly one `next_tool`, its
required preparation, exact project-aware review checklist schema, recovery behavior, and a stop-after-call flag.
`video_workflow status` returns the complete locked production brief for explicit resume/recovery.
Normal mutation results return only compact workflow metadata; they deliberately do not repeat the
full brief, preventing 64K-context saturation and stale-answer regressions in smaller local models.

The mandatory rule is: start the workflow before generating assets, execute only the returned next
action, and call status after any error. Do not grep implementation code, guess checklist types,
jump stages, or repeatedly mutate review payloads. Character-sheet checks are explicit booleans—
including `exact_character_count: true`—and reject duplicate views, insets/labels/swatches, and
cropped anatomy.

Every new brief must also persist an immutable Anima `style_bible` (verbatim positive prefix,
negative prompt, line grammar, cel shading, palette, background treatment, contrast, and color
temperature) plus `generation_lock` (checkpoint, sampler, steps, CFG, and resolution). The same
style strings and settings must be copied into every character-sheet and every required selective/
full storyboard; shot-specific wording is appended after the locked prefix.

## Pi tools

- `video_workflow`: start/status
- `video_define_brief`: expand and lock a vague request as a versioned production treatment
- `video_define_cut_plan`: persist and lock 1–15 second editorial cuts, start frames, actions, and local generation segments
- `video_submit_artifacts`: hash and attach visual artifacts for inspection
- `video_record_review`: persist structured cut-plan or visual artifact checklist pass/fail evidence
- `/video-workflow`: show current state

State defaults to `~/.pi/agent/state/create-video-guard.sqlite3`, keyed by the
actual Pi session UUID. Override with `PI_CREATE_VIDEO_GUARD_DB` for tests.

## Test

```bash
python3 -m unittest -v tests/test_workflow_state.py
node --test tests/extension_core.test.mjs
pi --offline --extension ./index.ts --list-models
```

## Install globally

```bash
ln -s /home/chihmin/src/create-video-guard \
  ~/.pi/agent/extensions/create-video-guard
```

Restart Pi or run `/reload` in the TUI.
