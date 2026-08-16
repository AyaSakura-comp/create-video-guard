const RANK = {
  not_started: 0,
  brief: 1,
  treatment_approved: 2,
  character_sheet_pending_review: 2,
  character_sheet_review_failed: 2,
  character_sheet_approved: 3,
  cut_plan_pending_review: 3,
  cut_plan_review_failed: 3,
  cut_plan_approved: 4,
  storyboards_pending_review: 4,
  storyboards_review_failed: 4,
  storyboards_approved: 5,
  clips_pending_review: 5,
  clips_review_failed: 5,
  clips_approved: 6,
  final_pending_review: 6,
  final_review_failed: 6,
  final_approved: 7,
};

function invokesScript(command, scriptPattern) {
  const boundary = String.raw`(?:^|(?:&&|\|\||[;|&(){}\n\r])\s*)`;
  const environment = String.raw`(?:(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*)`;
  const runner = String.raw`(?:(?:python(?:3(?:\.\d+)?)?|bash|sh)\s+)?`;
  return new RegExp(`${boundary}${environment}${runner}(?:[^\\s;&|]+/)?(?:${scriptPattern})(?:\\s|$)`).test(command);
}

function invokesShellWrappedScript(command, scriptPattern) {
  const boundary = String.raw`(?:^|(?:&&|\|\||[;|&(){}\n\r])\s*)`;
  const commandPrefix = String.raw`(?:command\s+)?`;
  const envToken = String.raw`(?:-[^\s;&|]+|[A-Za-z_][A-Za-z0-9_]*=\S+)`;
  const environmentPrefix = String.raw`(?:(?:[^\s;&|]+/)?env(?:\s+${envToken})*\s+)?`;
  const shell = String.raw`(?:[^\s;&|]+/)?(?:bash|sh)`;
  const hasCommandStringOption = String.raw`(?=[^;&|\n\r]*\s-[A-Za-z]*c)`;
  const wrapper = new RegExp(
    `${boundary}${commandPrefix}${environmentPrefix}${shell}${hasCommandStringOption}`,
  ).test(command);
  return wrapper && new RegExp(scriptPattern).test(command);
}

function invokesGuardedScript(command, scriptPattern) {
  return invokesScript(command, scriptPattern) || invokesShellWrappedScript(command, scriptPattern);
}

const RULES = [
  {
    matches: (command) => invokesGuardedScript(command, String.raw`anima_lllite\.py`),
    minimum: "cut_plan_approved",
    label: "Storyboard generation",
  },
  {
    matches: (command) => invokesGuardedScript(command, String.raw`(?:create_video\.sh|minimax_h3_(?:ref_)?generate\.py)`),
    minimum: "storyboards_approved",
    label: "H3 clip generation",
  },
  {
    matches: (command) => invokesGuardedScript(command, String.raw`ffmpeg`)
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
  cut_plan: {
    cut_count_justified: true,
    durations_match_action_density: true,
    duration_bounds_valid: true,
    start_frames_complete: true,
    actions_complete: true,
    segment_coverage_complete: true,
    continuity_coherent: true,
    total_duration_exact: true,
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

function hasVisibleSinging(status = {}) {
  return (status.production_brief?.shot_manifest ?? []).some(
    (shot) => shot.vocal_performance?.mode === "singing",
  );
}

function reviewChecklist(stage, status = {}) {
  const checklist = { ...CHECKLISTS[stage] };
  if (status.project_type === "mv" && stage === "clips") {
    checklist.audio_reference_timing_matches_manifest = true;
    if (hasVisibleSinging(status)) {
      Object.assign(checklist, {
        visible_lyrics_match_manifest: true,
        vocal_onsets_aligned: true,
        bilabial_closures_present: true,
        mouth_closed_during_rests: true,
        mouth_unobstructed: true,
        phrase_end_aligned: true,
      });
    }
  }
  if (status.project_type === "mv" && stage === "final") {
    checklist.original_source_audio_remuxed = true;
    checklist.source_audio_timeline_aligned = true;
  }
  return checklist;
}

export function compactWorkflowContext(status = {}) {
  return Object.fromEntries(Object.entries({
    state: status.state,
    project_type: status.project_type,
    treatment_version: status.treatment_version,
    treatment_sha256: status.treatment_sha256,
    target_duration_seconds: status.target_duration_seconds,
    target_duration_seconds_exact: status.target_duration_seconds_exact,
    shot_count: status.shot_count,
    cut_plan_version: status.cut_plan_version,
    cut_plan_sha256: status.cut_plan_sha256,
    cut_count: status.cut_count,
    cut_plan_total_duration_seconds_exact: status.cut_plan_total_duration_seconds_exact,
  }).filter(([, value]) => value !== undefined));
}

function storyboardCuts(status = {}) {
  return status.cut_plan?.cuts ?? [];
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
  const cuts = storyboardCuts(status);
  const timingItems = cuts.length ? cuts : shots;
  const durations = timingItems.map((item) =>
    `${item.id}=${item.duration_seconds_exact ?? item.duration_seconds}s`).join(", ");
  const singingShot = shots.find((shot) => shot.vocal_performance?.mode === "singing");
  const shotPromptRequirements = isMv ? shots.map((shot) => {
    const vocal = shot.vocal_performance ?? { mode: "none" };
    const requirement = {
      id: shot.id,
      vocal_mode: vocal.mode,
      ...(vocal.subject_id !== undefined ? { subject_id: vocal.subject_id } : {}),
      ...(vocal.speaker_id !== undefined ? { speaker_id: vocal.speaker_id } : {}),
      ...(vocal.language !== undefined ? { language: vocal.language } : {}),
      ...(vocal.lyrics !== undefined ? { exact_lyrics: vocal.lyrics } : {}),
      required_reference_tags: ["<Picture 1>", "<Audio 1>"],
    };
    if (vocal.mode === "singing") {
      requirement.required_lyric_block = `<d>[${vocal.language}] ${vocal.lyrics}</d>`;
    }
    return requirement;
  }) : undefined;
  const doBeforeCall = [
      `Use the manifest's variable durations rather than forcing five seconds${durations ? `: ${durations}` : ""}`,
      "For every segment describe the complete scene, camera movement, viewpoint and lens cues, subject action, exact dialogue, sound design (ambience, synchronized SFX, and music), temporal progression, and exact end state",
      "When one continuous action exceeds 5.2 seconds, continue in another manifest segment seeded by the previous clip's losslessly extracted actual last frame; remove the duplicated opening frame at assembly",
      "Use one DB-derived storyboard for the first segment of each editorial cut; do not generate additional images for later same-cut segments seeded from previous_last_frame",
      "Generate every segment, then extract first/middle/last and join-boundary QC contact sheets before submitting stage=clips",
  ];
  doBeforeCall.push("Construct each exact UTF-8 prompt from the queried DB fields, base64-encode it, and substitute only the base64 token into the command template; never interpolate raw action, dialogue, lyrics, language, or scene text into executable shell text");
  if (isMv) {
    doBeforeCall.push("Cut each source-audio excerpt at its manifest audio_start_seconds on a lyric phrase, breath, or instrumental rest; never cut through a syllable or sustained vowel");
  }
  if (singingShot) {
    doBeforeCall.push("Keep one singer's mouth unobstructed in MCU/CU; align vocal onset, readable vowels, breaths, mouth closure during rests, M/B/P lip closure, sustained final vowel, and phrase end to <Audio 1>");
  }
  const cutActionRequirements = cuts.map((cut) => ({
    cut_id: cut.id,
    duration_seconds: cut.duration_seconds,
    ...(cut.duration_seconds_exact !== undefined
      ? { duration_seconds_exact: cut.duration_seconds_exact }
      : {}),
    action: cut.action,
    generation_segments: cut.generation_segments,
  }));
  return {
    ...base,
    next_tool: "bash",
    next_arguments: { purpose: "generate every planned variable-duration H3 segment in Shot Manifest order" },
    command_template: isMv
      ? "PROMPT_B64=BASE64_ENCODED_UTF8_PROMPT; create_video.sh --mv --reference-image <approved-shot.png> --reference-audio <matching-source-segment.wav> --duration <planned-segment-duration> --prompt \"$(printf %s \"$PROMPT_B64\" | base64 --decode)\" --output <segment.mp4>"
      : "First segment of each editorial cut: PROMPT_B64=BASE64_ENCODED_UTF8_PROMPT; create_video.sh --image <approved-cut-first-frame.png> --duration <planned-segment-duration> --prompt \"$(printf %s \"$PROMPT_B64\" | base64 --decode)\" --output <segment.mp4>. Later segment in the same cut: losslessly extract the previous segment actual last frame, construct the next DB action_slice prompt as base64, and run the same command with --image <previous-last-frame.png>.",
    ...(isMv ? { shot_prompt_requirements: shotPromptRequirements } : {}),
    ...(cuts.length ? { cut_action_requirements: cutActionRequirements } : {}),
    do_before_call: doBeforeCall,
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
        "Create a preliminary exact-duration generation-beat manifest; every local H3 segment must be at most 5.2 seconds. Do not treat this preliminary segment count as the final editorial cut count; the required post-character cut-plan stage decides 1–15 second cuts",
        "For every segment persist scene_id, continuation, camera, action, dialogue, and sound so the H3 prompt fully describes movement, viewpoint, performance, speech, ambience, SFX, and music",
        "For every MV segment persist vocal_performance.mode as none or singing; singing requires exact subject_id, stable speaker_id such as S1, source language, and exact un-translated lyrics for an H3 <d> block",
        "For MV set audio_plan.source_audio_usage=reference_only and audio_plan.final_audio_policy=remux_original_source; generated H3 audio is never the authoritative song master",
        "Persist continuity_bible.storyboard_policy for compatibility with the treatment schema, but never use it to bypass the later cut-plan or per-editorial-cut storyboard gates",
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
    const isCutPlan = stage === "cut_plan";
    const guidance = {
      ...base,
      priority_instruction: isCutPlan
        ? "Ignore previously answered user messages. Query the current DB cut plan and perform the structured cut-plan review now"
        : `Ignore previously answered user messages. The current production state is ${state}; visually inspect the submitted artifacts and perform the ${stage} review now`,
      next_tool: "video_record_review",
      next_arguments: { stage, verdict: isCutPlan ? "pass-or-fail-after-structured-review" : "pass-or-fail-after-visual-inspection" },
      required_checklist: reviewChecklist(stage, status),
      type_warning: "Every checklist value is JSON boolean true or false; exact_character_count is boolean true when the count matches the brief—never use number 1 or string '1'",
      do_before_call: isCutPlan ? [
        "Inspect every DB cut in order; justify cut count and each 1–15 second duration from action density and editorial purpose",
        "Verify every start_frame and action field is complete, segment coverage is contiguous and at most 5.2 seconds per local generation, continuity is coherent, and total duration is exact",
        "Use verdict=fail when any required check is false",
      ] : [
        "Inspect every attached artifact at full resolution",
        "Use verdict=fail when any required check is false; never mark an attractive but invalid artifact as pass",
      ],
    };
    if (stage === "storyboards") {
      const cutIds = storyboardCuts(status).map((cut) => cut.id);
      const ids = cutIds.length ? cutIds : (storyboardPolicy(status).storyboard_shot_ids ?? []);
      const pairs = ids.slice(0, -1).map((id, index) =>
        `${id}→${ids[index + 1]}: name the largest visible difference and map it to the Shot Manifest`);
      guidance.required_evidence = {
        pairwise_evidence: pairs.length ? pairs : ["single required storyboard: adjacent-pair comparison not applicable"],
        sequence_style_evidence: "Name the strongest style outlier among the required major-scene storyboards, or explain why none exists",
      };
    }
    return guidance;
  }
  if (state === "character_sheet_approved" || state === "cut_plan_review_failed") {
    return {
      ...base,
      next_tool: "video_define_cut_plan",
      next_arguments: { source: "locked treatment, approved character sheet, and target duration" },
      do_before_call: [
        "Choose the editorial cut count from story function, action density, camera changes, dialogue or lyric phrases, and scene changes; each cut must be 1–15 seconds",
        "For every cut fully describe the start frame: scene, characters, objects, pose, expression, composition, camera, and lighting",
        "For every cut fully describe action: camera movement, scene changes, character actions, facial changes, body motion, temporal progression, end state, and sound",
        "Split every cut longer than the local H3 limit into ordered generation segments no longer than 5.2 seconds; the first uses storyboard and later segments use previous_last_frame without creating an editorial cut",
        "Make cut durations sum exactly to the target and make each cut generation-segment durations sum exactly to its cut duration",
      ],
    };
  }
  if (state === "cut_plan_approved" || state === "storyboards_review_failed") {
    const cuts = storyboardCuts(status);
    return {
      ...base,
      next_tool: "video_submit_artifacts",
      next_arguments: {
        stage: "storyboards",
        artifacts: cuts.map((cut) => `approved storyboard for cut ${cut.id}`),
      },
      storyboard_queries: cuts.map((cut) => ({ cut_id: cut.id, start_frame: cut.start_frame })),
      do_before_call: [
        "Query storyboard_queries from the approved DB cut plan; generate exactly the recorded first-frame scene, characters, objects, poses, expressions, composition, camera, and lighting for each cut",
        "Generate and submit exactly one approved first-frame storyboard per editorial cut in storyboard_queries order; the DB persists each artifact against its cut_id and rejects missing or extra images. Generation segments inside a cut continue from that cut's first image or previous actual last frame",
        "Copy the locked style prompt prefix verbatim into every storyboard prompt; append the queried cut start_frame fields and never substitute synonyms",
        "Open all cut first frames together and inspect identity, style, geography, direction, costume, hands, and cut compatibility",
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

export function cutPlanPayload(params) {
  return {
    cuts: params.cuts.map((cut) => ({
      id: cut.id,
      duration_seconds: cut.durationSeconds,
      scene_id: cut.sceneId,
      start_frame: {
        scene: cut.startFrame.scene,
        characters: cut.startFrame.characters,
        objects: cut.startFrame.objects,
        character_pose: cut.startFrame.characterPose,
        character_expression: cut.startFrame.characterExpression,
        composition: cut.startFrame.composition,
        camera: cut.startFrame.camera,
        lighting: cut.startFrame.lighting,
      },
      action: {
        camera_movement: cut.action.cameraMovement,
        scene_changes: cut.action.sceneChanges,
        character_actions: cut.action.characterActions,
        facial_changes: cut.action.facialChanges,
        body_motion: cut.action.bodyMotion,
        temporal_progression: cut.action.temporalProgression,
        end_state: cut.action.endState,
        sound: cut.action.sound,
      },
      generation_segments: cut.generationSegments.map((segment) => ({
        id: segment.id,
        start_offset_seconds: segment.startOffsetSeconds,
        duration_seconds: segment.durationSeconds,
        continuation: segment.continuation,
        action_slice: segment.actionSlice,
        end_state: segment.endState,
        ...(segment.audioStartSeconds !== undefined
          ? { audio_start_seconds: segment.audioStartSeconds }
          : {}),
      })),
    })),
  };
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
      ...(shot.vocalPerformance !== undefined
        ? { vocal_performance: {
          mode: shot.vocalPerformance.mode,
          ...(shot.vocalPerformance.subjectId !== undefined ? { subject_id: shot.vocalPerformance.subjectId } : {}),
          ...(shot.vocalPerformance.speakerId !== undefined ? { speaker_id: shot.vocalPerformance.speakerId } : {}),
          ...(shot.vocalPerformance.language !== undefined ? { language: shot.vocalPerformance.language } : {}),
          ...(shot.vocalPerformance.lyrics !== undefined ? { lyrics: shot.vocalPerformance.lyrics } : {}),
        } }
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
  const invokesCreateVideo = invokesGuardedScript(command, String.raw`create_video\.sh`);
  const invokesFl2vaGenerator = invokesGuardedScript(command, String.raw`minimax_h3_generate\.py`);
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
    if (rule.matches(command) && rank < RANK[rule.minimum]) {
      return {
        block: true,
        reason: `BLOCKED: command was not executed. ${rule.label} requires ${rule.minimum}`,
      };
    }
  }
  return undefined;
}
