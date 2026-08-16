import test from "node:test";
import assert from "node:assert/strict";

import {
  compactWorkflowContext,
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
    reason: "BLOCKED: command was not executed. Storyboard generation requires character_sheet_approved",
  });
});

test("allows storyboard generation after character sheet approval", () => {
  assert.equal(
    gateCommand("python anima_lllite.py 'shot'", "character_sheet_approved"),
    undefined,
  );
});

test("blocks H3 generation until storyboards are approved", () => {
  const result = gateCommand("./create_video.sh --prompt test", "storyboards_pending_review");
  assert.equal(result?.block, true);
  assert.match(result?.reason ?? "", /storyboards_approved/);
});

test("allows direct H3 generation after character approval for a same-scene plan", () => {
  assert.equal(gateCommand("./create_video.sh --prompt test", {
    state: "character_sheet_approved",
    production_brief: {
      continuity_bible: {
        storyboard_policy: { mode: "direct", storyboard_shot_ids: [] },
      },
    },
  }), undefined);
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

test("same-scene guidance skips image storyboards and chains variable segments from last frames", () => {
  const guidance = workflowGuidance({
    state: "character_sheet_approved",
    production_brief: {
      shot_manifest: [
        { id: "S01", scene_id: "well", duration_seconds: 3.1 },
        { id: "S02", scene_id: "well", duration_seconds: 4.4 },
      ],
      continuity_bible: {
        storyboard_policy: { mode: "direct", reason: "one continuous well scene", storyboard_shot_ids: [] },
      },
    },
  });
  assert.equal(guidance.next_tool, "bash");
  assert.match(guidance.command_template, /text-to-video/i);
  assert.match(guidance.command_template, /previous clip.*last frame/i);
  assert.match(guidance.do_before_call.join(" "), /camera.*viewpoint.*action.*dialogue.*sound/i);
  assert.match(guidance.do_before_call.join(" "), /3\.1.*4\.4/);
});

test("selective storyboard guidance requests only major scene-change images", () => {
  const guidance = workflowGuidance({
    state: "character_sheet_approved",
    production_brief: {
      continuity_bible: {
        storyboard_policy: { mode: "selective", reason: "one major location change", storyboard_shot_ids: ["S03"] },
      },
    },
  });
  assert.equal(guidance.next_tool, "video_submit_artifacts");
  assert.deepEqual(guidance.next_arguments, {
    stage: "storyboards",
    artifacts: ["approved storyboard for S03"],
  });
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
  const guidance = workflowGuidance({ state: "storyboards_approved", project_type: "mv" });
  assert.equal(guidance.next_tool, "bash");
  assert.match(guidance.command_template, /--mv/);
  assert.match(guidance.command_template, /--reference-image/);
  assert.match(guidance.command_template, /--reference-audio/);
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
    }],
    continuityBible: { direction: "left-to-right" },
    audioPlan: { ambience: "rain" },
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
    },
  ]);
  assert.deepEqual(payload.agent_assumptions[0], {
    assumption: "No dialogue", basis: "Not requested", confidence: "medium",
  });
});
