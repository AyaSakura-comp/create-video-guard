const RANK = {
  not_started: 0,
  brief: 1,
  treatment_approved: 2,
  character_sheet_pending_review: 2,
  character_sheet_review_failed: 2,
  character_sheet_approved: 3,
  storyboards_pending_review: 3,
  storyboards_review_failed: 3,
  storyboards_approved: 4,
  clips_pending_review: 4,
  clips_review_failed: 4,
  clips_approved: 5,
  final_pending_review: 5,
  final_review_failed: 5,
  final_approved: 6,
};

function invokesScript(command, scriptPattern) {
  const boundary = String.raw`(?:^|(?:&&|\|\||[;|&(){}\n\r])\s*)`;
  const environment = String.raw`(?:(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*)`;
  const runner = String.raw`(?:(?:python(?:3(?:\.\d+)?)?|bash|sh)\s+)?`;
  return new RegExp(`${boundary}${environment}${runner}(?:[^\\s;&|]+/)?(?:${scriptPattern})(?:\\s|$)`).test(command);
}

const RULES = [
  {
    matches: (command) => invokesScript(command, String.raw`anima_lllite\.py`),
    minimum: "character_sheet_approved",
    label: "Storyboard generation",
  },
  {
    matches: (command) => invokesScript(command, String.raw`(?:create_video\.sh|minimax_h3_(?:ref_)?generate\.py)`),
    minimum: "storyboards_approved",
    label: "H3 clip generation",
  },
  {
    matches: (command) => invokesScript(command, String.raw`ffmpeg`)
      && /(?:-f\s+concat|concat:|filter_complex[\s\S]*concat)/.test(command),
    minimum: "clips_approved",
    label: "Final video assembly",
  },
];

const CHECKLISTS = {
  character_sheet: {
    exact_character_count: true,
    full_body_visible: true,
    identity_features_consistent: true,
    pure_white_background: true,
    no_duplicates_or_extras: true,
    single_view_per_character: true,
    no_insets_labels_or_swatches: true,
    anatomy_uncropped: true,
  },
  storyboards: {
    all_planned_shots_present: true,
    identity_consistent: true,
    composition_matches_shot_map: true,
    line_weight_consistent: true,
    cel_shading_consistent: true,
    palette_temperature_consistent: true,
    background_rendering_consistent: true,
    scene_geography_consistent: true,
    screen_direction_eyelines_consistent: true,
    props_costume_hands_consistent: true,
    adjacent_cuts_compatible: true,
    style_outliers_absent: true,
  },
  clips: {
    identity_consistent: true,
    motion_matches_intent: true,
    no_visual_artifacts: true,
    continuity_preserved: true,
  },
  final: {
    joins_clean: true,
    audiovisual_sync: true,
    style_consistent: true,
    exact_duration: true,
  },
};

const BASE_RECOVERY = "On any tool error, call video_workflow with action=status and follow its next action; do not grep or inspect implementation code";

export function compactWorkflowContext(status = {}) {
  return Object.fromEntries(Object.entries({
    state: status.state,
    project_type: status.project_type,
    treatment_version: status.treatment_version,
    treatment_sha256: status.treatment_sha256,
    target_duration_seconds: status.target_duration_seconds,
    shot_count: status.shot_count,
  }).filter(([, value]) => value !== undefined));
}

function storyboardPolicy(status = {}) {
  return status.production_brief?.continuity_bible?.storyboard_policy ?? {
    mode: "full",
    reason: "legacy brief defaults to full storyboard coverage",
    storyboard_shot_ids: status.production_brief?.shot_manifest?.map((shot) => shot.id)
      ?? Array.from({ length: status.shot_count ?? 0 }, (_, index) => `S${String(index + 1).padStart(2, "0")}`),
  };
}

function clipGenerationGuidance(status, base) {
  const isMv = status.project_type === "mv";
  const shots = status.production_brief?.shot_manifest ?? [];
  const durations = shots.map((shot) => `${shot.id}=${shot.duration_seconds}s`).join(", ");
  return {
    ...base,
    next_tool: "bash",
    next_arguments: { purpose: "generate every planned variable-duration H3 segment in Shot Manifest order" },
    command_template: isMv
      ? "create_video.sh --mv --reference-image <approved-shot.png> --reference-audio <matching-source-segment.wav> --duration <planned-segment-duration> --prompt 'Use <Picture 1> ... Use <Audio 1> ...' --output <segment.mp4>"
      : "First segment: text-to-video with create_video.sh --duration <planned-segment-duration> --prompt '<complete scene, camera, viewpoint, action, dialogue, ambience, SFX, music, and end state>'. Same-scene continuation: losslessly extract the previous clip actual last frame, then create_video.sh --image <previous-last-frame.png> --duration <planned-segment-duration> --prompt '<continue established motion and sound>' --output <segment.mp4>. Use an approved storyboard first frame only for a listed major scene change.",
    do_before_call: [
      `Use the manifest's variable durations rather than forcing five seconds${durations ? `: ${durations}` : ""}`,
      "For every segment describe the complete scene, camera movement, viewpoint and lens cues, subject action, exact dialogue, sound design (ambience, synchronized SFX, and music), temporal progression, and exact end state",
      "When one continuous action exceeds 5.2 seconds, continue in another manifest segment seeded by the previous clip's losslessly extracted actual last frame; remove the duplicated opening frame at assembly",
      "Do not generate an image storyboard for an unchanged scene; use one only for storyboard_shot_ids that mark major scene/geography changes",
      "Generate every segment, then extract first/middle/last and join-boundary QC contact sheets before submitting stage=clips",
    ],
  };
}

export function workflowGuidance(status = {}) {
  const state = status.state ?? "not_started";
  const base = { state, stop_after_tool_call: true, on_error: BASE_RECOVERY };
  if (state === "not_started") {
    return { ...base, next_tool: "video_workflow", next_arguments: { action: "start" } };
  }
  if (state === "brief") {
    return {
      ...base,
      next_tool: "video_define_brief",
      next_arguments: { preserve_user_wording_exactly: true },
      do_before_call: [
        "Classify an existing-song music video as projectType=mv; otherwise use narrative or other",
        "Create an exact-duration Shot Manifest of variable-duration H3 segments; every segment must be at most 5.2 seconds, and actions longer than that continue in another segment using previous_last_frame",
        "For every segment persist scene_id, continuation, camera, action, dialogue, and sound so the H3 prompt fully describes movement, viewpoint, performance, speech, ambience, SFX, and music",
        "Set continuity_bible.storyboard_policy: direct for one unchanged scene, selective for only major scene/geography changes, or full only when explicitly/technically required; list only required image shots in storyboard_shot_ids",
        "Separate explicit requirements, attributed assumptions, and creative choices",
        "Lock a style bible with one exact positive prompt prefix and negative prompt; describe line grammar, cel shading, palette, background rendering, contrast, and color temperature",
        "Lock checkpoint, sampler, steps, CFG, and resolution in generation_lock; for Anima Base prefer er_sde, 30 steps, CFG 4-5 unless a tested project requirement says otherwise",
      ],
    };
  }
  if (state === "treatment_approved" || state === "character_sheet_review_failed") {
    return {
      ...base,
      next_tool: "video_submit_artifacts",
      next_arguments: { stage: "character_sheet", artifacts: ["/absolute/path/to/verified-character-sheet.png"] },
      do_before_call: [
        "Prepend the locked style prompt prefix verbatim and use the locked negative prompt and generation settings; never paraphrase the style block between assets",
        "Generate exactly one front-facing neutral full-body view per recurring character on pure white",
        "Keep every hand, foot, ear, tail, costume edge, and prop fully visible and uncropped",
        "Use no inset, no duplicate view, no turnaround, no label, no swatch, no scenery, and no overlap",
        "Open and visually inspect the full-resolution file; regenerate instead of submitting if any rule fails",
      ],
    };
  }
  if (state.endsWith("_pending_review")) {
    const stage = state.slice(0, -"_pending_review".length);
    const guidance = {
      ...base,
      priority_instruction: `Ignore previously answered user messages. The current production state is ${state}; visually inspect the submitted artifacts and perform the ${stage} review now`,
      next_tool: "video_record_review",
      next_arguments: { stage, verdict: "pass-or-fail-after-visual-inspection" },
      required_checklist: CHECKLISTS[stage],
      type_warning: "Every checklist value is JSON boolean true or false; exact_character_count is boolean true when the count matches the brief—never use number 1 or string '1'",
      do_before_call: [
        "Inspect every attached artifact at full resolution",
        "Use verdict=fail when any required check is false; never mark an attractive but invalid artifact as pass",
      ],
    };
    if (stage === "storyboards") {
      const ids = storyboardPolicy(status).storyboard_shot_ids ?? [];
      const pairs = ids.slice(0, -1).map((id, index) =>
        `${id}→${ids[index + 1]}: name the largest visible difference and map it to the Shot Manifest`);
      guidance.required_evidence = {
        pairwise_evidence: pairs.length ? pairs : ["single required storyboard: adjacent-pair comparison not applicable"],
        sequence_style_evidence: "Name the strongest style outlier among the required major-scene storyboards, or explain why none exists",
      };
    }
    return guidance;
  }
  if (state === "character_sheet_approved" || state === "storyboards_review_failed") {
    const policy = storyboardPolicy(status);
    if (state === "character_sheet_approved" && policy.mode === "direct") {
      return clipGenerationGuidance(status, base);
    }
    const ids = policy.storyboard_shot_ids ?? [];
    return {
      ...base,
      next_tool: "video_submit_artifacts",
      next_arguments: {
        stage: "storyboards",
        artifacts: ids.map((id) => `approved storyboard for ${id}`),
      },
      do_before_call: [
        `Generate image storyboards only for these major scene/geography changes: ${ids.join(", ")}`,
        "Do not generate image storyboards for unchanged-scene continuation segments; they will use the previous video's actual last frame",
        "Copy the locked style prompt prefix verbatim into every required storyboard prompt; append shot-specific content after it and never substitute synonyms",
        "Open the required frames together and inspect identity, style, geography, direction, costume, hands, and cut compatibility",
      ],
    };
  }
  if (state === "storyboards_approved") {
    return clipGenerationGuidance(status, base);
  }
  if (state === "clips_review_failed") {
    return {
      ...base,
      next_tool: "video_submit_artifacts",
      next_arguments: { stage: "clips", artifacts: ["regenerated clip QC frames/contact sheets"] },
      do_before_call: ["Regenerate only failed takes, then inspect first/middle/last frames and joins"],
    };
  }
  if (state === "clips_approved" || state === "final_review_failed") {
    return {
      ...base,
      next_tool: "bash",
      next_arguments: { purpose: "assemble exact-duration final and extract final/join QC contact sheets" },
      do_before_call: [
        "Assemble in Shot Manifest order with editorial cuts and exact target duration",
        "For MV, replace generated audio with the untouched original source song on the final timeline",
        "Call video_submit_artifacts with stage=final and visual QC artifacts before delivery",
      ],
    };
  }
  if (state === "final_approved") {
    return { ...base, next_tool: "deliver", do_before_call: ["Attach the approved final video and report exact duration, resolution, fps, and audio stream"] };
  }
  return { ...base, next_tool: "video_workflow", next_arguments: { action: "status" } };
}

export function productionBriefPayload(params) {
  return {
    user_request: params.userRequest,
    project_type: params.projectType,
    ...(params.sourceAudioPath ? { source_audio_path: params.sourceAudioPath } : {}),
    target_duration_seconds: params.targetDurationSeconds,
    explicit_requirements: params.explicitRequirements,
    agent_assumptions: params.agentAssumptions,
    creative_choices: params.creativeChoices,
    treatment: params.treatment,
    shot_manifest: params.shotManifest.map((shot) => ({
      id: shot.id,
      duration_seconds: shot.durationSeconds,
      beat: shot.beat,
      scene_id: shot.sceneId,
      continuation: shot.continuation,
      camera: shot.camera,
      action: shot.action,
      dialogue: shot.dialogue,
      sound: shot.sound,
      ...(shot.audioStartSeconds !== undefined
        ? { audio_start_seconds: shot.audioStartSeconds }
        : {}),
    })),
    continuity_bible: params.continuityBible,
    audio_plan: params.audioPlan,
  };
}

export function gateCommand(command, stateOrStatus) {
  const status = typeof stateOrStatus === "string" ? { state: stateOrStatus } : stateOrStatus;
  const state = status?.state ?? "not_started";
  const rank = RANK[state] ?? 0;
  const invokesCreateVideo = invokesScript(command, String.raw`create_video\.sh`);
  const invokesFl2vaGenerator = invokesScript(command, String.raw`minimax_h3_generate\.py`);
  if (status?.project_type === "mv" && (invokesCreateVideo || invokesFl2vaGenerator)) {
    const usesMvR2v = !invokesFl2vaGenerator
      && /(?:^|\s)--mv(?:\s|$)/.test(command)
      && /(?:^|\s)--reference-image(?:=|\s)/.test(command)
      && /(?:^|\s)--reference-audio(?:=|\s)/.test(command);
    if (!usesMvR2v) {
      return {
        block: true,
        reason: "BLOCKED: command was not executed. MV requires reference-image + reference-audio R2V workflow",
      };
    }
  }
  for (const rule of RULES) {
    const directAfterCharacter = rule.label === "H3 clip generation"
      && state === "character_sheet_approved"
      && storyboardPolicy(status).mode === "direct";
    if (rule.matches(command) && rank < RANK[rule.minimum] && !directAfterCharacter) {
      return {
        block: true,
        reason: `BLOCKED: command was not executed. ${rule.label} requires ${rule.minimum}`,
      };
    }
  }
  return undefined;
}
