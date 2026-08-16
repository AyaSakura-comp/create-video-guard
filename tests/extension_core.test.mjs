import test from "node:test";
import assert from "node:assert/strict";

import {
  compactWorkflowContext,
  cutPlanPayload,
  gateCommand,
  productionBriefPayload,
  workflowGuidance,
} from "../extension_core.mjs";

test("blocks storyboard generation before character sheet approval", () => {
  const result = gateCommand(
    "python ~/.pi/agent/skills/create-image/scripts/anima_lllite.py 'shot'",
    "brief",
  );
  assert.deepEqual(result, {
    block: true,
    reason: "BLOCKED: command was not executed. Storyboard generation requires cut_plan_approved",
  });
});

test("blocks storyboard generation until the cut plan is approved", () => {
  const beforePlan = gateCommand("python anima_lllite.py 'shot'", "character_sheet_approved");
  assert.equal(beforePlan?.block, true);
  assert.match(beforePlan?.reason ?? "", /cut_plan_approved/);
  assert.equal(
    gateCommand("python anima_lllite.py 'shot'", "cut_plan_approved"),
    undefined,
  );
});

test("blocks H3 generation until storyboards are approved", () => {
  const result = gateCommand("./create_video.sh --prompt test", "storyboards_pending_review");
  assert.equal(result?.block, true);
  assert.match(result?.reason ?? "", /storyboards_approved/);
});

test("blocks direct H3 generation until cut planning and storyboards are approved", () => {
  const result = gateCommand("./create_video.sh --prompt test", {
    state: "character_sheet_approved",
    production_brief: {
      continuity_bible: {
        storyboard_policy: { mode: "direct", storyboard_shot_ids: [] },
      },
    },
  });
  assert.equal(result?.block, true);
  assert.match(result?.reason ?? "", /storyboards_approved/);
});

test("blocks final concat until clips are approved", () => {
  const result = gateCommand(
    "ffmpeg -f concat -i clips.txt -c copy final.mp4",
    "clips_pending_review",
  );
  assert.equal(result?.block, true);
  assert.match(result?.reason ?? "", /clips_approved/);
});

test("does not interfere with unrelated shell commands", () => {
  assert.equal(gateCommand("git status", "brief"), undefined);
});

test("does not mistake source inspection for execution of a guarded generator", () => {
  assert.equal(gateCommand("rg -n negative anima_lllite.py", "brief"), undefined);
  assert.equal(gateCommand("git diff -- create_video.sh", "brief"), undefined);
});

test("blocks guarded generators behind multiline and shell control operators", () => {
  for (const command of [
    "cd /tmp\npython /opt/anima_lllite.py --prompt test",
    "printf ready | python /opt/anima_lllite.py --prompt test",
    "(python /opt/anima_lllite.py --prompt test)",
    "sleep 1 & python /opt/anima_lllite.py --prompt test",
    "{ python /opt/anima_lllite.py --prompt test; }",
  ]) {
    assert.match(gateCommand(command, "brief")?.reason ?? "", /BLOCKED/);
  }
});

test("blocks guarded generators hidden inside shell -c wrappers", () => {
  for (const command of [
    "bash -c './create_video.sh --prompt test'",
    "sh -c 'python /opt/minimax_h3_generate.py --image board.png'",
    "bash -c \"python /opt/anima_lllite.py --prompt board\"",
    "/bin/bash -c './create_video.sh --prompt test'",
    "bash --noprofile -c './create_video.sh --prompt test'",
    "command bash -c './create_video.sh --prompt test'",
    "env -i bash -c './create_video.sh --prompt test'",
    "/usr/bin/env -i /bin/bash -c './create_video.sh --prompt test'",
    "/bin/bash -lc './create_video.sh --prompt test'",
    "sh -xc 'python /opt/anima_lllite.py --prompt board'",
    "bash -c'./create_video.sh --prompt test'",
    "env bash -c'python anima_lllite.py --prompt board'",
    "command sh -c'python minimax_h3_generate.py --image board.png'",
    "bash$IFS-c$IFS'./create_video.sh --prompt test'",
  ]) {
    const result = gateCommand(command, "brief");
    assert.equal(result?.block, true, command);
    assert.match(result?.reason ?? "", /BLOCKED/, command);
  }
});

test("blocks first-frame workflow for an MV production", () => {
  const result = gateCommand(
    "./create_video.sh --image board.png -p shot",
    { state: "storyboards_approved", project_type: "mv" },
  );
  assert.equal(result?.block, true);
  assert.match(result?.reason ?? "", /MV requires reference-image \+ reference-audio R2V/);
});

test("allows R2V image and audio references for an approved MV", () => {
  assert.equal(
    gateCommand(
      "./create_video.sh --mv --reference-image board.png --reference-audio song.wav -p shot",
      { state: "storyboards_approved", project_type: "mv" },
    ),
    undefined,
  );
});

test("blocks direct FL2VA generator bypass for an MV", () => {
  const result = gateCommand(
    "python minimax_h3_generate.py --image board.png",
    { state: "storyboards_approved", project_type: "mv" },
  );
  assert.equal(result?.block, true);
  assert.match(result?.reason ?? "", /MV requires reference-image \+ reference-audio R2V/);
});

test("character approval requires a normalized cut plan before storyboard generation", () => {
  const guidance = workflowGuidance({
    state: "character_sheet_approved",
    target_duration_seconds: 8,
  });
  assert.equal(guidance.next_tool, "video_define_cut_plan");
  assert.match(guidance.do_before_call.join(" "), /1.*15.*seconds/i);
  assert.match(guidance.do_before_call.join(" "), /start frame/i);
  assert.match(guidance.do_before_call.join(" "), /generation segment.*5\.2/i);
});

test("cut-plan review exposes duration, start-frame, action, and segment checks", () => {
  const guidance = workflowGuidance({ state: "cut_plan_pending_review" });
  assert.equal(guidance.next_tool, "video_record_review");
  assert.deepEqual(guidance.required_checklist, {
    cut_count_justified: true,
    durations_match_action_density: true,
    duration_bounds_valid: true,
    start_frames_complete: true,
    actions_complete: true,
    segment_coverage_complete: true,
    continuity_coherent: true,
    total_duration_exact: true,
  });
});

test("storyboard guidance queries each approved cut start frame", () => {
  const guidance = workflowGuidance({
    state: "cut_plan_approved",
    cut_plan: {
      cuts: [
        { id: "C01", start_frame: { scene: "moonlit station", characters: "rabbit courier" } },
        { id: "C02", start_frame: { scene: "train roof", characters: "rabbit courier" } },
      ],
    },
  });
  assert.equal(guidance.next_tool, "video_submit_artifacts");
  assert.deepEqual(guidance.next_arguments.artifacts, [
    "approved storyboard for cut C01",
    "approved storyboard for cut C02",
  ]);
  assert.deepEqual(guidance.storyboard_queries, [
    { cut_id: "C01", start_frame: { scene: "moonlit station", characters: "rabbit courier" } },
    { cut_id: "C02", start_frame: { scene: "train roof", characters: "rabbit courier" } },
  ]);
});

test("video guidance queries cut actions and generation segments", () => {
  const action = {
    camera_movement: "slow push in", scene_changes: "rain intensifies",
    character_actions: "rabbit raises letter", facial_changes: "fear to resolve",
    body_motion: "shoulders settle", temporal_progression: "raise then hold",
    end_state: "letter fills foreground", sound: "song and rain",
  };
  const segments = [{
    id: "C01-G01", start_offset_seconds: 0, duration_seconds: 4,
    continuation: "storyboard", action_slice: "raise letter", end_state: "letter raised",
  }];
  const guidance = workflowGuidance({
    state: "storyboards_approved",
    project_type: "narrative",
    cut_plan: { cuts: [{ id: "C01", duration_seconds: 4, action, generation_segments: segments }] },
  });
  assert.deepEqual(guidance.cut_action_requirements, [{
    cut_id: "C01", duration_seconds: 4, action, generation_segments: segments,
  }]);
});

test("narrative cut actions stay out of executable shell text", () => {
  const guidance = workflowGuidance({
    state: "storyboards_approved",
    project_type: "narrative",
    cut_plan: { cuts: [{
      id: "C01", duration_seconds: 3,
      action: {
        camera_movement: "push in'; touch /tmp/injected #",
        scene_changes: "none", character_actions: "wave",
        facial_changes: "smile", body_motion: "raise hand",
        temporal_progression: "raise then hold", end_state: "hand raised", sound: "wind",
      },
      generation_segments: [{
        id: "C01-G01", start_offset_seconds: 0, duration_seconds: 3,
        continuation: "storyboard", action_slice: "wave", end_state: "hand raised",
      }],
    }] },
  });
  assert.doesNotMatch(guidance.command_template, /touch \/tmp\/injected|push in/);
  assert.match(guidance.command_template, /base64/i);
  assert.match(guidance.do_before_call.join(" "), /base64.*never.*raw/i);
});

test("maps normalized cut-plan fields to persisted snake_case", () => {
  const payload = cutPlanPayload({
    cuts: [{
      id: "C01", durationSeconds: 6, sceneId: "station",
      startFrame: {
        scene: "moonlit station", characters: "rabbit courier at center",
        objects: "red mailbox", characterPose: "feet planted",
        characterExpression: "alert", composition: "medium wide",
        camera: "eye level 35mm", lighting: "cool moonlight",
      },
      action: {
        cameraMovement: "push in", sceneChanges: "rain strengthens",
        characterActions: "rabbit opens letter", facialChanges: "alert to shocked",
        bodyMotion: "hands unfold paper", temporalProgression: "open then read",
        endState: "letter held at chest", sound: "rain and paper rustle",
      },
      generationSegments: [
        { id: "C01-G01", startOffsetSeconds: 0, durationSeconds: 3,
          continuation: "storyboard", actionSlice: "open letter", endState: "letter open" },
        { id: "C01-G02", startOffsetSeconds: 3, durationSeconds: 3,
          continuation: "previous_last_frame", actionSlice: "read letter", endState: "shocked" },
      ],
    }],
  });
  assert.equal(payload.cuts[0].duration_seconds, 6);
  assert.equal(payload.cuts[0].start_frame.character_pose, "feet planted");
  assert.equal(payload.cuts[0].action.camera_movement, "push in");
  assert.equal(payload.cuts[0].generation_segments[1].start_offset_seconds, 3);
});

test("guidance gives one deterministic next action after treatment approval", () => {
  const guidance = workflowGuidance({ state: "treatment_approved", shot_count: 3 });
  assert.equal(guidance.next_tool, "video_submit_artifacts");
  assert.equal(guidance.next_arguments.stage, "character_sheet");
  assert.match(guidance.do_before_call.join(" "), /pure white/i);
  assert.match(guidance.do_before_call.join(" "), /locked style prompt prefix/i);
  assert.match(guidance.do_before_call.join(" "), /no inset/i);
  assert.equal(guidance.stop_after_tool_call, true);
});

test("brief guidance locks an immutable Anima style bible and generation settings", () => {
  const guidance = workflowGuidance({ state: "brief" });
  assert.match(guidance.do_before_call.join(" "), /style bible/i);
  assert.match(guidance.do_before_call.join(" "), /positive prompt prefix/i);
  assert.match(guidance.do_before_call.join(" "), /sampler.*steps.*CFG.*resolution/i);
});

test("mutation results keep only compact workflow context instead of repeating the full brief", () => {
  const context = compactWorkflowContext({
    state: "character_sheet_pending_review",
    project_type: "narrative",
    treatment_version: 1,
    treatment_sha256: "abc",
    target_duration_seconds: 12,
    shot_count: 4,
    production_brief: { treatment: { huge: "x".repeat(100_000) } },
  });
  assert.deepEqual(context, {
    state: "character_sheet_pending_review",
    project_type: "narrative",
    treatment_version: 1,
    treatment_sha256: "abc",
    target_duration_seconds: 12,
    shot_count: 4,
  });
  assert.equal("production_brief" in context, false);
});

test("character-sheet review guidance makes every required field explicitly boolean", () => {
  const guidance = workflowGuidance({ state: "character_sheet_pending_review" });
  assert.match(guidance.priority_instruction, /ignore previously answered.*review now/i);
  assert.equal(guidance.next_tool, "video_record_review");
  assert.deepEqual(guidance.required_checklist, {
    exact_character_count: true,
    full_body_visible: true,
    identity_features_consistent: true,
    pure_white_background: true,
    no_duplicates_or_extras: true,
    single_view_per_character: true,
    no_insets_labels_or_swatches: true,
    anatomy_uncropped: true,
  });
  assert.match(guidance.type_warning, /boolean true.*never.*number/i);
});

test("storyboard review guidance exposes written evidence requirements", () => {
  const guidance = workflowGuidance({ state: "storyboards_pending_review", shot_count: 2 });
  assert.equal(guidance.next_tool, "video_record_review");
  assert.deepEqual(guidance.required_evidence, {
    pairwise_evidence: ["S01→S02: name the largest visible difference and map it to the Shot Manifest"],
    sequence_style_evidence: "Name the strongest style outlier among the required major-scene storyboards, or explain why none exists",
  });
});

test("MV clip guidance requires the official image plus audio R2V route", () => {
  const guidance = workflowGuidance({
    state: "storyboards_approved",
    project_type: "mv",
    production_brief: {
      shot_manifest: [{
        id: "S01", duration_seconds: 3.75,
        vocal_performance: {
          mode: "singing", subject_id: "Subject 1", speaker_id: "S1",
          language: "Japanese", lyrics: "ここにいるよ",
        },
      }],
    },
  });
  assert.equal(guidance.next_tool, "bash");
  assert.match(guidance.command_template, /--mv/);
  assert.match(guidance.command_template, /--reference-image/);
  assert.match(guidance.command_template, /--reference-audio/);
  assert.match(guidance.command_template, /base64/i);
  assert.deepEqual(guidance.shot_prompt_requirements, [{
    id: "S01",
    vocal_mode: "singing",
    subject_id: "Subject 1",
    speaker_id: "S1",
    language: "Japanese",
    exact_lyrics: "ここにいるよ",
    required_reference_tags: ["<Picture 1>", "<Audio 1>"],
    required_lyric_block: "<d>[Japanese] ここにいるよ</d>",
  }]);
  assert.match(guidance.do_before_call.join(" "), /mouth.*rests.*M\/B\/P/i);
});

test("MV guidance keeps mixed per-shot vocal metadata out of executable shell text", () => {
  const unsafeLanguage = "English'; echo injected #";
  const guidance = workflowGuidance({
    state: "storyboards_approved",
    project_type: "mv",
    production_brief: {
      shot_manifest: [
        {
          id: "S01", duration_seconds: 3,
          vocal_performance: {
            mode: "singing", subject_id: "Subject 1", speaker_id: "S1",
            language: "Japanese", lyrics: "一番",
          },
        },
        { id: "S02", duration_seconds: 3, vocal_performance: { mode: "none" } },
        {
          id: "S03", duration_seconds: 3,
          vocal_performance: {
            mode: "singing", subject_id: "Subject 2", speaker_id: "S2",
            language: unsafeLanguage, lyrics: "second line",
          },
        },
      ],
    },
  });
  assert.doesNotMatch(guidance.command_template, /echo injected|Japanese|second line|\(S1\)/);
  assert.match(guidance.command_template, /base64/i);
  assert.match(guidance.do_before_call.join(" "), /base64.*never.*raw.*lyrics/i);
  assert.deepEqual(guidance.shot_prompt_requirements.map((shot) => ({
    id: shot.id,
    mode: shot.vocal_mode,
    speaker: shot.speaker_id,
    lyrics: shot.exact_lyrics,
  })), [
    { id: "S01", mode: "singing", speaker: "S1", lyrics: "一番" },
    { id: "S02", mode: "none", speaker: undefined, lyrics: undefined },
    { id: "S03", mode: "singing", speaker: "S2", lyrics: "second line" },
  ]);
});

test("MV singing clip review exposes hard lip-sync checks", () => {
  const guidance = workflowGuidance({
    state: "clips_pending_review",
    project_type: "mv",
    production_brief: {
      shot_manifest: [{ vocal_performance: { mode: "singing" } }],
    },
  });
  assert.equal(guidance.required_checklist.audio_reference_timing_matches_manifest, true);
  assert.equal(guidance.required_checklist.visible_lyrics_match_manifest, true);
  assert.equal(guidance.required_checklist.vocal_onsets_aligned, true);
  assert.equal(guidance.required_checklist.bilabial_closures_present, true);
  assert.equal(guidance.required_checklist.mouth_closed_during_rests, true);
  assert.equal(guidance.required_checklist.mouth_unobstructed, true);
  assert.equal(guidance.required_checklist.phrase_end_aligned, true);
});

test("MV final review requires authoritative original-song remux checks", () => {
  const guidance = workflowGuidance({
    state: "final_pending_review",
    project_type: "mv",
  });
  assert.equal(guidance.required_checklist.original_source_audio_remuxed, true);
  assert.equal(guidance.required_checklist.source_audio_timeline_aligned, true);
});

test("failed reviews tell the model to regenerate instead of searching implementation code", () => {
  const guidance = workflowGuidance({ state: "character_sheet_review_failed" });
  assert.match(guidance.on_error, /do not grep|do not inspect implementation/i);
  assert.equal(guidance.next_tool, "video_submit_artifacts");
});

test("maps agent-expanded vague requirements to the persisted production brief", () => {
  const payload = productionBriefPayload({
    userRequest: "A dancer on a moonlit stage",
    projectType: "mv",
    sourceAudioPath: "/tmp/song.mp3",
    targetDurationSeconds: 5,
    explicitRequirements: ["dancer"],
    agentAssumptions: [{ assumption: "No dialogue", basis: "Not requested", confidence: "medium" }],
    creativeChoices: ["Two-shot structure"],
    treatment: { logline: "Complete the performance" },
    shotManifest: [{
      id: "S01", durationSeconds: 5, beat: "final pose", sceneId: "stage",
      continuation: "storyboard", camera: "slow low-angle push-in",
      action: "dancer lands in final pose", dialogue: "none",
      sound: "rain ambience and applause", audioStartSeconds: 0,
      vocalPerformance: {
        mode: "singing", subjectId: "Subject 1", speakerId: "S1",
        language: "Japanese", lyrics: "ここにいるよ",
      },
    }],
    continuityBible: { direction: "left-to-right" },
    audioPlan: {
      ambience: "rain", source_audio_usage: "reference_only",
      final_audio_policy: "remux_original_source",
    },
  });
  assert.equal(payload.user_request, "A dancer on a moonlit stage");
  assert.equal(payload.project_type, "mv");
  assert.equal(payload.source_audio_path, "/tmp/song.mp3");
  assert.equal(payload.target_duration_seconds, 5);
  assert.deepEqual(payload.shot_manifest, [
    {
      id: "S01", duration_seconds: 5, beat: "final pose", scene_id: "stage",
      continuation: "storyboard", camera: "slow low-angle push-in",
      action: "dancer lands in final pose", dialogue: "none",
      sound: "rain ambience and applause", audio_start_seconds: 0,
      vocal_performance: {
        mode: "singing", subject_id: "Subject 1", speaker_id: "S1",
        language: "Japanese", lyrics: "ここにいるよ",
      },
    },
  ]);
  assert.deepEqual(payload.agent_assumptions[0], {
    assumption: "No dialogue", basis: "Not requested", confidence: "medium",
  });
});
