import { spawn } from "node:child_process";
import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, extname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { StringEnum, Type } from "@earendil-works/pi-ai";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

import {
  compactWorkflowContext,
  cutPlanPayload,
  gateCommand,
  productionBriefPayload,
  workflowGuidance,
} from "./extension_core.mjs";

const ROOT = dirname(fileURLToPath(import.meta.url));
const STATE_SCRIPT = resolve(ROOT, "scripts/workflow_state.py");
const DB_PATH = process.env.PI_CREATE_VIDEO_GUARD_DB
  ?? resolve(homedir(), ".pi/agent/state/create-video-guard.sqlite3");
const artifactStages = ["character_sheet", "storyboards", "clips", "final"] as const;
const reviewStages = ["character_sheet", "cut_plan", "storyboards", "clips", "final"] as const;
const verdicts = ["pass", "fail"] as const;

interface StateResult {
  session_id: string;
  state: string;
  [key: string]: unknown;
}

function runState(sessionId: string, args: string[]): Promise<StateResult> {
  return new Promise((resolvePromise, reject) => {
    const child = spawn("python3", [STATE_SCRIPT, "--db", DB_PATH, "--session", sessionId, ...args], {
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("error", reject);
    child.on("close", (code) => {
      const raw = code === 0 ? stdout : stderr;
      try {
        const parsed = JSON.parse(raw.trim());
        if (code === 0) resolvePromise(parsed);
        else reject(new Error(JSON.stringify(parsed)));
      } catch {
        reject(new Error(`create-video state command failed (${code}): ${raw.trim()}`));
      }
    });
  });
}

function sessionId(ctx: ExtensionContext): string {
  return ctx.sessionManager.getSessionId();
}

async function runGuidedState(id: string, args: string[]): Promise<StateResult> {
  const result = await runState(id, args);
  const status = args[0] === "status" ? result : await runState(id, ["status"]);
  return {
    ...result,
    ...(args[0] === "status" ? {} : { workflow_context: compactWorkflowContext(status) }),
    workflow_guidance: workflowGuidance(status),
  };
}

function textResult(result: StateResult) {
  return {
    content: [{ type: "text" as const, text: JSON.stringify(result, null, 2) }],
    details: result,
  };
}

async function artifactContent(paths: string[]) {
  const content: Array<
    | { type: "text"; text: string }
    | { type: "image"; data: string; mimeType: string }
  > = [];
  const mimeTypes: Record<string, string> = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
  };
  for (const path of paths.slice(0, 12)) {
    const mimeType = mimeTypes[extname(path).toLowerCase()];
    if (!mimeType) continue;
    content.push({ type: "image", data: (await readFile(path)).toString("base64"), mimeType });
  }
  return content;
}

export default function createVideoGuard(pi: ExtensionAPI) {
  pi.registerTool({
    name: "video_workflow",
    label: "Video workflow",
    description: "MANDATORY first tool for every video/animation request. Start or inspect the SQLite workflow. The result contains workflow_guidance with exactly one next action; follow it and stop, and call status after any tool error instead of searching source code.",
    parameters: Type.Object({ action: StringEnum(["start", "status"] as const) }),
    async execute(_id, params, _signal, _update, ctx) {
      return textResult(await runGuidedState(sessionId(ctx), [params.action]));
    },
  });

  pi.registerTool({
    name: "video_define_brief",
    label: "Define production brief",
    description:
      "Expand the user's original, possibly vague request into a traceable production treatment, shot manifest, continuity bible, and audio plan. Preserve explicit requirements separately from agent assumptions and creative choices. This locks the plan required before character-sheet review.",
    parameters: Type.Object({
      userRequest: Type.String({ minLength: 1, description: "Exact original wording from the user" }),
      projectType: StringEnum(["narrative", "mv", "other"] as const),
      sourceAudioPath: Type.Optional(Type.String({ description: "Required existing MP3/WAV path for MV projects" })),
      targetDurationSeconds: Type.Number({ exclusiveMinimum: 0 }),
      explicitRequirements: Type.Array(Type.String()),
      agentAssumptions: Type.Array(Type.Object({
        assumption: Type.String({ minLength: 1 }),
        basis: Type.String({ minLength: 1, description: "Why the agent inferred this from the request" }),
        confidence: StringEnum(["low", "medium", "high"] as const),
      })),
      creativeChoices: Type.Array(Type.String()),
      treatment: Type.Unknown({ description: "JSON object containing logline, beats, emotional arc, setting, and direction" }),
      shotManifest: Type.Array(Type.Object({
        id: Type.String({ minLength: 1 }),
        durationSeconds: Type.Number({ exclusiveMinimum: 0 }),
        beat: Type.String({ minLength: 1 }),
        sceneId: Type.String({ minLength: 1, description: "Stable scene/location id; reuse it while geography is unchanged" }),
        continuation: StringEnum(["none", "previous_last_frame", "storyboard"] as const, { description: "Use previous_last_frame for same-scene continuation; storyboard only at major scene/geography changes" }),
        camera: Type.String({ minLength: 1, description: "Shot size, viewpoint, lens cues, movement path, speed, and endpoint" }),
        action: Type.String({ minLength: 1, description: "Observable subject action and temporal progression" }),
        dialogue: Type.String({ minLength: 1, description: "Exact quoted dialogue or 'none'" }),
        sound: Type.String({ minLength: 1, description: "Ambience, synchronized SFX, and music progression" }),
        audioStartSeconds: Type.Optional(Type.Number({ minimum: 0, description: "Required source-audio in-point for each MV shot" })),
        vocalPerformance: Type.Optional(Type.Object({
          mode: StringEnum(["none", "singing"] as const, { description: "Required for every MV shot: none when no visible singer, singing when a visible subject performs the source vocal" }),
          subjectId: Type.Optional(Type.String({ minLength: 1, description: "Required for singing; stable H3 subject id such as Subject 1" })),
          speakerId: Type.Optional(Type.String({ minLength: 1, description: "Required for singing; stable H3 speaker id such as S1" })),
          language: Type.Optional(Type.String({ minLength: 1, description: "Required for singing; original source-lyric language" })),
          lyrics: Type.Optional(Type.String({ minLength: 1, description: "Required for singing; exact untranslated source lyrics for the H3 <d> block; use [unclear] rather than inventing words" })),
        }, { description: "Required for every MV shot; singing fields are validated conditionally" })),
      }), { minItems: 1 }),
      continuityBible: Type.Unknown({ description: "JSON object locking geography/direction/props/costume plus storyboard_policy {mode: direct|selective|full, reason, storyboard_shot_ids}. Use direct for one unchanged scene, selective only for major scene/geography changes, and full only when explicitly or technically required. Also include style_bible {positive_prompt_prefix, negative_prompt, line_grammar, cel_shading, palette, background_rendering, contrast, color_temperature} and generation_lock {checkpoint, sampler, steps, cfg, resolution}." }),
      audioPlan: Type.Unknown({ description: "JSON object for dialogue, ambience, SFX, and music. MV requires source_audio_usage='reference_only' and final_audio_policy='remux_original_source' because generated H3 audio is not the authoritative song master." }),
    }),
    async execute(_id, params, _signal, _update, ctx) {
      const brief = productionBriefPayload(params);
      return textResult(await runGuidedState(sessionId(ctx), [
        "define-brief", "--brief-json", JSON.stringify(brief),
      ]));
    },
  });

  pi.registerTool({
    name: "video_define_cut_plan",
    label: "Define editorial cut plan",
    description:
      "Required after character-sheet approval and before storyboard generation. Decide the total number of editorial cuts and each 1–15 second cut duration from action density and story rhythm. Persist each cut's complete first-frame contents, full camera/environment/character action, and ordered local H3 generation segments. Cuts may be up to 15 seconds, but every local generation segment must be at most 5.2 seconds.",
    parameters: Type.Object({
      cuts: Type.Array(Type.Object({
        id: Type.String({ minLength: 1, description: "Stable editorial cut id such as C01" }),
        durationSeconds: Type.Number({ minimum: 1, maximum: 15, description: "Editorial cut duration; must match action density" }),
        sceneId: Type.String({ minLength: 1 }),
        startFrame: Type.Object({
          scene: Type.String({ minLength: 1, description: "Complete location, background, weather, and environmental state visible in frame one" }),
          characters: Type.String({ minLength: 1, description: "Every visible character, identity, costume, position, orientation, and count" }),
          objects: Type.String({ minLength: 1, description: "Every important prop, foreground/midground/background object, and landmark" }),
          characterPose: Type.String({ minLength: 1, description: "Exact body pose, feet, hands, weight, gaze, and contact state at frame one" }),
          characterExpression: Type.String({ minLength: 1, description: "Exact facial expression and mouth/eye state at frame one" }),
          composition: Type.String({ minLength: 1, description: "Framing, screen positions, depth layers, eyelines, and negative space" }),
          camera: Type.String({ minLength: 1, description: "Initial shot size, viewpoint, height, angle, lens cues, and focus" }),
          lighting: Type.String({ minLength: 1, description: "Light sources, direction, color temperature, contrast, and shadows" }),
        }),
        action: Type.Object({
          cameraMovement: Type.String({ minLength: 1, description: "Movement type, direction/path, amplitude, speed, subject relationship, and endpoint" }),
          sceneChanges: Type.String({ minLength: 1, description: "How environment, objects, weather, particles, and lighting evolve" }),
          characterActions: Type.String({ minLength: 1, description: "Every character's ordered observable actions and interactions" }),
          facialChanges: Type.String({ minLength: 1, description: "Ordered expression, gaze, blink, and mouth changes" }),
          bodyMotion: Type.String({ minLength: 1, description: "Ordered pose, limb, hand, foot, torso, balance, and contact mechanics" }),
          temporalProgression: Type.String({ minLength: 1, description: "Beginning, progression, turning point, and settle timing" }),
          endState: Type.String({ minLength: 1, description: "Exact final frame scene, camera, pose, expression, objects, light, and sound state" }),
          sound: Type.String({ minLength: 1, description: "Dialogue/lyrics, ambience, synchronized SFX, and music progression" }),
        }),
        generationSegments: Type.Array(Type.Object({
          id: Type.String({ minLength: 1, description: "Stable generation segment id such as C01-G01" }),
          startOffsetSeconds: Type.Number({ minimum: 0 }),
          durationSeconds: Type.Number({ exclusiveMinimum: 0, maximum: 5.2 }),
          continuation: StringEnum(["storyboard", "previous_last_frame"] as const, { description: "First segment is storyboard; later same-cut segments are previous_last_frame" }),
          actionSlice: Type.String({ minLength: 1, description: "The observable subset of the cut action generated in this segment" }),
          endState: Type.String({ minLength: 1, description: "Exact segment endpoint used to continue the next segment" }),
          audioStartSeconds: Type.Optional(Type.Number({ minimum: 0, description: "Required for every MV segment" })),
        }), { minItems: 1 }),
      }), { minItems: 1 }),
    }),
    async execute(_id, params, _signal, _update, ctx) {
      const plan = cutPlanPayload(params);
      return textResult(await runGuidedState(sessionId(ctx), [
        "define-cut-plan", "--cut-plan-json", JSON.stringify(plan),
      ]));
    },
  });

  pi.registerTool({
    name: "video_submit_artifacts",
    label: "Submit video artifacts",
    description:
      "Submit only the stage named by workflow_guidance. Character sheets must be pure-white, one full-body view per character, with no duplicate views/insets/labels/swatches/crops. Submit the complete storyboard batch. For clips/final, submit first/middle/last and join QC contact sheets so visual inspection is possible.",
    parameters: Type.Object({
      stage: StringEnum(artifactStages),
      artifacts: Type.Array(Type.String({ description: "Absolute path to an existing visual artifact" }), { minItems: 1 }),
    }),
    async execute(_id, params, _signal, _update, ctx) {
      const result = await runGuidedState(sessionId(ctx), [
        "submit", "--stage", params.stage,
        ...params.artifacts.flatMap((path: string) => ["--artifact", path]),
      ]);
      return {
        content: [
          { type: "text" as const, text: `${JSON.stringify(result, null, 2)}\nVisually inspect every attached image, then call video_record_review.` },
          ...await artifactContent(params.artifacts),
        ],
        details: result,
      };
    },
  });

  pi.registerTool({
    name: "video_record_review",
    label: "Record visual review",
    description:
      "Record the pending structured or visual pass/fail only after inspecting the current DB cut plan or every submitted artifact. Get the exact project-aware checklist from video_workflow status. Cut plans require justified count/durations, complete start frames/actions, contiguous generation-segment coverage, continuity, and exact total timing. MV singing clips add lyric, vocal-onset, M/B/P closure, rest-closure, mouth-visibility, and phrase-end checks; MV finals require original-song remux and timeline checks. Every required value is JSON boolean true/false; exact_character_count is a boolean match check, NEVER the number 1. Fail invalid work rather than retrying schemas or searching code.",
    parameters: Type.Object({
      stage: StringEnum(reviewStages),
      verdict: StringEnum(verdicts),
      checklistJson: Type.String({ description: "Copy the complete project-aware required_checklist from video_workflow status. Every check is JSON boolean true/false; exact_character_count is boolean (true when count matches), never numeric. Storyboard passes additionally require non-empty pairwise_evidence[] and sequence_style_evidence." }),
      reason: Type.String({ minLength: 1 }),
    }),
    async execute(_id, params, _signal, _update, ctx) {
      const reviewer = ctx.model ? `${ctx.model.provider}/${ctx.model.id}` : "unknown";
      return textResult(await runGuidedState(sessionId(ctx), [
        "review", "--stage", params.stage,
        "--verdict", params.verdict,
        "--checklist-json", params.checklistJson,
        "--reason", params.reason,
        "--reviewer", reviewer,
      ]));
    },
  });

  pi.on("tool_call", async (event, ctx) => {
    if (event.toolName !== "bash") return undefined;
    const command = String((event.input as { command?: unknown }).command ?? "");
    const status = await runState(sessionId(ctx), ["status"]);
    return gateCommand(command, status);
  });

  pi.on("session_start", async (_event, ctx) => {
    const status = await runState(sessionId(ctx), ["status"]);
    ctx.ui.setStatus("create-video-guard", `video: ${status.state}`);
  });

  pi.registerCommand("video-workflow", {
    description: "Show the create-video visual-gate state for this session",
    handler: async (_args, ctx) => {
      const status = await runState(sessionId(ctx), ["status"]);
      ctx.ui.notify(`Create-video workflow: ${status.state}`, "info");
    },
  });
}
