# create-video-guard

Pi extension that enforces a SQLite-backed visual-review state machine for the
`create-video` workflow.

## Stages

`vague brief → treatment → character_sheet → (direct clips | selective storyboards) → clips → final`

The user's original wording is preserved while the agent fills in the production details:
explicit requirements, attributed assumptions, creative choices, treatment, exact-duration
Shot Manifest, continuity bible, and audio plan. This versioned treatment must reach
`treatment_approved` before a character sheet can be submitted.

For `project_type=mv`, `video_define_brief` additionally requires an existing MP3/WAV source,
hashes it, and requires each 2–5.2 second shot to declare its source-audio in-point. After
storyboard approval, the shell gate rejects first-frame FL2VA and requires the local R2V route:
`create_video.sh --mv --reference-image ... --reference-audio ...`. The vendored official ComfyUI
template is `~/.pi/agent/skills/create-video/workflows/video_minimax_h3_r2v.json`; the executable
API graph is implemented by `~/src/ComfyUI/scripts/minimax_h3_ref_generate.py`.

Every brief persists `storyboard_policy`:

- `direct`: one unchanged scene; skip image storyboards after character approval.
- `selective`: generate images only for listed major scene/geography changes.
- `full`: all segments need images, reserved for explicit/technical requirements such as MV R2V.

Shot Manifest entries are variable-duration H3 segments up to 5.2 seconds and include `scene_id`,
`continuation`, complete camera/viewpoint, action, dialogue, and sound plans. Longer same-scene
actions are split and use the previous clip's actual lossless PNG last frame as the next first
frame. Direct mode permits H3 and clip submission immediately after character-sheet approval and
blocks unnecessary storyboard submission. Every stage that is actually required is still submitted
and visually reviewed before the next guarded operation.

## Deterministic small-model guidance

Every successful workflow tool result includes `workflow_guidance`: exactly one `next_tool`, its
required preparation, exact review checklist schema, recovery behavior, and a stop-after-call flag.
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
- `video_submit_artifacts`: hash and attach visual artifacts for inspection
- `video_record_review`: persist checklist-based pass/fail evidence
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
