#!/usr/bin/env python3
"""SQLite-backed workflow state for the Pi create-video guard."""

import argparse
import hashlib
import json
import math
import re
import sqlite3
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ARTIFACT_STAGES = ("character_sheet", "storyboards", "clips", "final")
REVIEW_STAGES = ("character_sheet", "cut_plan", "storyboards", "clips", "final")
REQUIRED_STATE = {
    "character_sheet": {"treatment_approved", "character_sheet_review_failed"},
    "storyboards": {"cut_plan_approved", "storyboards_review_failed"},
    "clips": {"storyboards_approved", "clips_review_failed"},
    "final": {"clips_approved", "final_review_failed"},
}
LOCK_MESSAGE_STATE = {
    "character_sheet": "treatment_approved",
    "storyboards": "cut_plan_approved",
    "clips": "storyboards_approved",
    "final": "clips_approved",
}
CHECKLISTS = {
    "character_sheet": (
        "exact_character_count", "full_body_visible", "identity_features_consistent",
        "pure_white_background", "no_duplicates_or_extras",
        "single_view_per_character", "no_insets_labels_or_swatches",
        "anatomy_uncropped",
    ),
    "cut_plan": (
        "cut_count_justified", "durations_match_action_density",
        "duration_bounds_valid", "start_frames_complete", "actions_complete",
        "segment_coverage_complete", "continuity_coherent", "total_duration_exact",
    ),
    "storyboards": (
        "all_planned_shots_present", "identity_consistent",
        "composition_matches_shot_map", "line_weight_consistent",
        "cel_shading_consistent", "palette_temperature_consistent",
        "background_rendering_consistent", "scene_geography_consistent",
        "screen_direction_eyelines_consistent", "props_costume_hands_consistent",
        "adjacent_cuts_compatible", "style_outliers_absent",
    ),
    "clips": (
        "identity_consistent", "motion_matches_intent", "no_visual_artifacts",
        "continuity_preserved",
    ),
    "final": ("joins_clean", "audiovisual_sync", "style_consistent", "exact_duration"),
}
MV_CLIP_CHECKS = ("audio_reference_timing_matches_manifest",)
MV_SINGING_CLIP_CHECKS = (
    "visible_lyrics_match_manifest", "vocal_onsets_aligned",
    "bilabial_closures_present", "mouth_closed_during_rests",
    "mouth_unobstructed", "phrase_end_aligned",
)
MV_FINAL_CHECKS = ("original_source_audio_remuxed", "source_audio_timeline_aligned")


def required_checklist(brief, stage):
    fields = list(CHECKLISTS[stage])
    if isinstance(brief, dict) and brief.get("project_type") == "mv":
        if stage == "clips":
            fields.extend(MV_CLIP_CHECKS)
            if any(
                isinstance(shot, dict)
                and isinstance(shot.get("vocal_performance"), dict)
                and shot["vocal_performance"].get("mode") == "singing"
                for shot in brief.get("shot_manifest", [])
            ):
                fields.extend(MV_SINGING_CLIP_CHECKS)
        elif stage == "final":
            fields.extend(MV_FINAL_CHECKS)
    return tuple(fields)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit(payload, *, error=False):
    stream = sys.stderr if error else sys.stdout
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=stream)


def fail(code: str, **details):
    emit({"error": code, **details}, error=True)
    raise SystemExit(2)


def canonical_decimal_json(value):
    if isinstance(value, dict):
        return "{" + ",".join(
            f"{json.dumps(str(key), ensure_ascii=False)}:{canonical_decimal_json(value[key])}"
            for key in sorted(value)
        ) + "}"
    if isinstance(value, list):
        return "[" + ",".join(canonical_decimal_json(item) for item in value) + "]"
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite JSON number")
        rendered = format(value, "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        return "0" if rendered in ("-0", "") else rendered
    if value is None or isinstance(value, (str, bool, int, float)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    raise TypeError(f"unsupported canonical JSON type: {type(value).__name__}")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE IF NOT EXISTS workflow_sessions (
            session_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS production_treatments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            brief_json TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(session_id, version),
            FOREIGN KEY(session_id) REFERENCES workflow_sessions(session_id)
        );
        CREATE TABLE IF NOT EXISTS cut_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            plan_json TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            cut_count INTEGER NOT NULL,
            total_duration_seconds REAL NOT NULL,
            total_duration_text TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(session_id, version),
            FOREIGN KEY(session_id) REFERENCES workflow_sessions(session_id)
        );
        CREATE TABLE IF NOT EXISTS cut_plan_items (
            session_id TEXT NOT NULL,
            plan_version INTEGER NOT NULL,
            cut_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            duration_seconds REAL NOT NULL,
            duration_seconds_text TEXT,
            scene_id TEXT NOT NULL,
            start_frame_json TEXT NOT NULL,
            action_json TEXT NOT NULL,
            generation_segments_json TEXT NOT NULL,
            PRIMARY KEY(session_id, plan_version, cut_id),
            FOREIGN KEY(session_id) REFERENCES workflow_sessions(session_id)
        );
        CREATE TABLE IF NOT EXISTS stage_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            artifact_key TEXT,
            path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            submitted_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES workflow_sessions(session_id)
        );
        CREATE TABLE IF NOT EXISTS visual_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            verdict TEXT NOT NULL,
            checklist_json TEXT NOT NULL,
            reason TEXT NOT NULL,
            reviewer TEXT,
            reviewed_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES workflow_sessions(session_id)
        );
        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            event TEXT NOT NULL,
            from_state TEXT,
            to_state TEXT NOT NULL,
            details_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES workflow_sessions(session_id)
        );
        """
    )
    artifact_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(stage_artifacts)").fetchall()
    }
    if "artifact_key" not in artifact_columns:
        conn.execute("ALTER TABLE stage_artifacts ADD COLUMN artifact_key TEXT")
    cut_plan_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(cut_plans)").fetchall()
    }
    if "total_duration_text" not in cut_plan_columns:
        conn.execute("ALTER TABLE cut_plans ADD COLUMN total_duration_text TEXT")
    cut_item_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(cut_plan_items)").fetchall()
    }
    if "duration_seconds_text" not in cut_item_columns:
        conn.execute("ALTER TABLE cut_plan_items ADD COLUMN duration_seconds_text TEXT")
    return conn


def get_row(conn, session_id):
    return conn.execute(
        "SELECT session_id, state, created_at, updated_at FROM workflow_sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()


def transition(conn, session_id, event, from_state, to_state, details):
    timestamp = now()
    conn.execute(
        "UPDATE workflow_sessions SET state = ?, updated_at = ? WHERE session_id = ?",
        (to_state, timestamp, session_id),
    )
    conn.execute(
        "INSERT INTO audit_events(session_id,event,from_state,to_state,details_json,created_at) VALUES(?,?,?,?,?,?)",
        (session_id, event, from_state, to_state, json.dumps(details, sort_keys=True), timestamp),
    )


def command_start(conn, session_id):
    row = get_row(conn, session_id)
    if row is None:
        timestamp = now()
        conn.execute(
            "INSERT INTO workflow_sessions(session_id,state,created_at,updated_at) VALUES(?,?,?,?)",
            (session_id, "brief", timestamp, timestamp),
        )
        conn.execute(
            "INSERT INTO audit_events(session_id,event,from_state,to_state,details_json,created_at) VALUES(?,?,?,?,?,?)",
            (session_id, "start", None, "brief", "{}", timestamp),
        )
        conn.commit()
    return command_status(conn, session_id)


def command_status(conn, session_id):
    row = get_row(conn, session_id)
    if row is None:
        return {"session_id": session_id, "state": "not_started"}
    result = dict(row)
    treatment = conn.execute(
        "SELECT version, brief_json, sha256 FROM production_treatments "
        "WHERE session_id = ? ORDER BY version DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    if treatment:
        brief = json.loads(treatment["brief_json"])
        exact_brief = json.loads(
            treatment["brief_json"],
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=Decimal,
        )
        for shot, exact_shot in zip(brief["shot_manifest"], exact_brief["shot_manifest"]):
            shot["duration_seconds_exact"] = canonical_decimal_json(exact_shot["duration_seconds"])
            if "audio_start_seconds" in exact_shot:
                shot["audio_start_seconds_exact"] = canonical_decimal_json(exact_shot["audio_start_seconds"])
        target_exact = canonical_decimal_json(exact_brief["target_duration_seconds"])
        result.update({
            "treatment_version": treatment["version"],
            "treatment_sha256": treatment["sha256"],
            "project_type": brief["project_type"],
            "target_duration_seconds": brief["target_duration_seconds"],
            "target_duration_seconds_exact": target_exact,
            "shot_count": len(brief["shot_manifest"]),
            "production_brief": brief,
        })
    cut_plan_row = conn.execute(
        "SELECT version, plan_json, sha256, cut_count, total_duration_seconds, total_duration_text FROM cut_plans "
        "WHERE session_id = ? ORDER BY version DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    if cut_plan_row:
        item_rows = conn.execute(
            "SELECT cut_id, duration_seconds, duration_seconds_text, scene_id, start_frame_json, action_json, generation_segments_json "
            "FROM cut_plan_items WHERE session_id = ? AND plan_version = ? ORDER BY ordinal",
            (session_id, cut_plan_row["version"]),
        ).fetchall()
        queried_cuts = []
        for item in item_rows:
            segments = json.loads(item["generation_segments_json"])
            exact_segments = json.loads(
                item["generation_segments_json"],
                parse_float=str,
                parse_int=str,
            )
            for segment, exact_segment in zip(segments, exact_segments):
                segment["start_offset_seconds_exact"] = str(exact_segment["start_offset_seconds"])
                segment["duration_seconds_exact"] = str(exact_segment["duration_seconds"])
                if "audio_start_seconds" in exact_segment:
                    segment["audio_start_seconds_exact"] = str(exact_segment["audio_start_seconds"])
            queried_cuts.append({
                "id": item["cut_id"],
                "duration_seconds": item["duration_seconds"],
                "duration_seconds_exact": item["duration_seconds_text"] or str(item["duration_seconds"]),
                "scene_id": item["scene_id"],
                "start_frame": json.loads(item["start_frame_json"]),
                "action": json.loads(item["action_json"]),
                "generation_segments": segments,
            })
        queried_plan = {"cuts": queried_cuts}
        result.update({
            "cut_plan_version": cut_plan_row["version"],
            "cut_plan_sha256": cut_plan_row["sha256"],
            "cut_count": cut_plan_row["cut_count"],
            "cut_plan_total_duration_seconds": cut_plan_row["total_duration_seconds"],
            "cut_plan_total_duration_seconds_exact": cut_plan_row["total_duration_text"] or str(cut_plan_row["total_duration_seconds"]),
            "cut_plan": queried_plan,
        })
    return result


def storyboard_policy(brief):
    continuity = brief.get("continuity_bible", {}) if isinstance(brief, dict) else {}
    policy = continuity.get("storyboard_policy") if isinstance(continuity, dict) else None
    if isinstance(policy, dict):
        return policy
    # Legacy persisted briefs predate selective storyboards and retain the old full gate.
    shots = brief.get("shot_manifest", []) if isinstance(brief, dict) else []
    return {
        "mode": "full",
        "reason": "legacy brief defaults to full storyboard coverage",
        "storyboard_shot_ids": [shot.get("id") for shot in shots if isinstance(shot, dict)],
    }


def latest_production_brief(conn, session_id):
    row = conn.execute(
        "SELECT brief_json FROM production_treatments WHERE session_id = ? ORDER BY version DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return json.loads(row["brief_json"]) if row else None


def validate_production_brief(brief, exact_brief):
    required = (
        "user_request", "project_type", "target_duration_seconds", "explicit_requirements",
        "agent_assumptions", "creative_choices", "treatment", "shot_manifest",
        "continuity_bible", "audio_plan",
    )
    missing = [key for key in required if key not in brief]
    if missing:
        fail("invalid_production_brief", detail=f"missing fields: {', '.join(missing)}")
    if not isinstance(brief["user_request"], str) or not brief["user_request"].strip():
        fail("invalid_production_brief", detail="user_request must be non-empty")
    if brief["project_type"] not in ("narrative", "mv", "other"):
        fail("invalid_production_brief", detail="project_type must be narrative, mv, or other")
    assumptions = brief["agent_assumptions"]
    if not isinstance(assumptions, list) or any(
        not isinstance(item, dict)
        or not all(isinstance(item.get(key), str) and item[key].strip() for key in ("assumption", "basis"))
        or item.get("confidence") not in ("low", "medium", "high")
        for item in assumptions
    ):
        fail(
            "invalid_production_brief",
            detail="agent_assumptions must contain assumption, basis, and low/medium/high confidence",
        )
    target = brief["target_duration_seconds"]
    exact_target = exact_brief["target_duration_seconds"]
    if (
        not isinstance(target, (int, float)) or isinstance(target, bool)
        or not math.isfinite(target) or target <= 0
        or not exact_target.is_finite() or exact_target <= Decimal("0")
    ):
        fail("invalid_production_brief", detail="target_duration_seconds must be a finite positive number")
    shots = brief["shot_manifest"]
    if not isinstance(shots, list) or not shots:
        fail("invalid_production_brief", detail="shot_manifest must contain at least one shot")
    if any(
        not isinstance(shot, dict)
        or not isinstance(shot.get("id"), str) or not shot["id"].strip()
        or not isinstance(shot.get("beat"), str) or not shot["beat"].strip()
        or not isinstance(shot.get("duration_seconds"), (int, float))
        or isinstance(shot.get("duration_seconds"), bool)
        or not math.isfinite(shot["duration_seconds"])
        or shot["duration_seconds"] <= 0
        for shot in shots
    ):
        fail("invalid_production_brief", detail="each shot needs id, beat, and positive duration_seconds")
    shot_ids = [shot["id"] for shot in shots]
    if len(set(shot_ids)) != len(shot_ids):
        fail("invalid_production_brief", detail="shot ids must be unique")
    exact_shots = exact_brief["shot_manifest"]
    if any(
        not shot["duration_seconds"].is_finite()
        or shot["duration_seconds"] <= Decimal("0")
        for shot in exact_shots
    ):
        fail("invalid_production_brief", detail="each shot duration_seconds must be a finite positive number")
    exact_shot_total = sum(
        (shot["duration_seconds"] for shot in exact_shots),
        Decimal("0"),
    )
    if exact_shot_total != exact_target:
        fail("invalid_production_brief", detail="shot durations must sum exactly to target_duration_seconds")
    if any(shot["duration_seconds"] > Decimal("5.2") for shot in exact_shots):
        fail(
            "invalid_production_brief",
            detail="each H3 segment must be at most 5.2 seconds; split longer action into variable-duration segments and set later same-scene segments to continuation=previous_last_frame",
        )
    prompt_fields = ("scene_id", "continuation", "camera", "action", "dialogue", "sound")
    if any(
        any(not isinstance(shot.get(key), str) or not shot[key].strip() for key in prompt_fields)
        or shot.get("continuation") not in ("none", "previous_last_frame", "storyboard")
        for shot in shots
    ):
        fail(
            "invalid_production_brief",
            detail="each shot requires scene_id, continuation, camera, action, dialogue, and sound for a complete direct H3 prompt",
        )
    continuity = brief["continuity_bible"]
    style_fields = (
        "positive_prompt_prefix", "negative_prompt", "line_grammar", "cel_shading",
        "palette", "background_rendering", "contrast", "color_temperature",
    )
    lock_fields = ("checkpoint", "sampler", "steps", "cfg", "resolution")
    style_bible = continuity.get("style_bible") if isinstance(continuity, dict) else None
    generation_lock = continuity.get("generation_lock") if isinstance(continuity, dict) else None
    policy = continuity.get("storyboard_policy") if isinstance(continuity, dict) else None
    if (
        not isinstance(policy, dict)
        or policy.get("mode") not in ("direct", "selective", "full")
        or not isinstance(policy.get("reason"), str) or not policy["reason"].strip()
        or not isinstance(policy.get("storyboard_shot_ids"), list)
        or any(not isinstance(item, str) or not item.strip() for item in policy.get("storyboard_shot_ids", []))
    ):
        fail(
            "invalid_production_brief",
            detail="continuity_bible requires storyboard_policy with mode direct/selective/full, reason, and storyboard_shot_ids",
        )
    storyboard_ids = policy["storyboard_shot_ids"]
    if len(storyboard_ids) != len(set(storyboard_ids)) or any(item not in shot_ids for item in storyboard_ids):
        fail("invalid_production_brief", detail="storyboard_shot_ids must be unique valid Shot Manifest ids")
    if policy["mode"] == "direct" and (
        storyboard_ids
        or len({shot["scene_id"] for shot in shots}) != 1
        or shots[0]["continuation"] != "none"
        or any(shot["continuation"] != "previous_last_frame" for shot in shots[1:])
    ):
        fail(
            "invalid_production_brief",
            detail="direct storyboard policy requires one scene, no storyboard ids, first continuation=none, and every later segment continuation=previous_last_frame",
        )
    if policy["mode"] == "selective" and not storyboard_ids:
        fail("invalid_production_brief", detail="selective storyboard policy requires at least one major scene-change storyboard id")
    if policy["mode"] == "full" and set(storyboard_ids) != set(shot_ids):
        fail("invalid_production_brief", detail="full storyboard policy requires every Shot Manifest id")
    if any((shot["id"] in storyboard_ids) != (shot["continuation"] == "storyboard") for shot in shots):
        fail("invalid_production_brief", detail="storyboard_shot_ids must exactly match shots with continuation=storyboard")
    if brief["project_type"] == "mv" and policy["mode"] != "full":
        fail("invalid_production_brief", detail="MV R2V requires full storyboard coverage for picture plus source-audio references")
    if (
        not isinstance(style_bible, dict)
        or any(not isinstance(style_bible.get(key), str) or not style_bible[key].strip() for key in style_fields)
        or not isinstance(generation_lock, dict)
        or any(key not in generation_lock for key in lock_fields)
        or any(not isinstance(generation_lock.get(key), str) or not generation_lock[key].strip()
               for key in ("checkpoint", "sampler", "resolution"))
        or not isinstance(generation_lock.get("steps"), int)
        or isinstance(generation_lock.get("steps"), bool)
        or generation_lock["steps"] <= 0
        or not isinstance(generation_lock.get("cfg"), (int, float))
        or isinstance(generation_lock.get("cfg"), bool)
        or not math.isfinite(generation_lock["cfg"])
        or generation_lock["cfg"] <= 0
    ):
        fail(
            "invalid_production_brief",
            detail="continuity_bible requires complete style_bible and generation_lock fields",
        )
    if brief["project_type"] == "mv":
        raw_audio = brief.get("source_audio_path")
        if not isinstance(raw_audio, str) or not raw_audio.strip():
            fail("invalid_production_brief", detail="MV requires source_audio_path")
        audio_path = Path(raw_audio).expanduser().resolve()
        if not audio_path.is_file() or audio_path.suffix.lower() not in (".mp3", ".wav"):
            fail("invalid_production_brief", detail="source_audio_path must be an existing MP3 or WAV")
        if any(
            not isinstance(shot.get("audio_start_seconds"), (int, float))
            or isinstance(shot.get("audio_start_seconds"), bool)
            or not math.isfinite(shot["audio_start_seconds"])
            for shot in shots
        ) or any(
            shot.get("audio_start_seconds", Decimal("-1")) < 0
            or shot["duration_seconds"] < Decimal("2")
            or shot["duration_seconds"] > Decimal("5.2")
            for shot in exact_shots
        ):
            fail(
                "invalid_production_brief",
                detail="MV shots require audio_start_seconds and local 2–5.2 second durations",
            )
        audio_plan = brief.get("audio_plan")
        if (
            not isinstance(audio_plan, dict)
            or audio_plan.get("source_audio_usage") != "reference_only"
            or audio_plan.get("final_audio_policy") != "remux_original_source"
        ):
            fail(
                "invalid_production_brief",
                detail="MV audio_plan requires source_audio_usage=reference_only and final_audio_policy=remux_original_source",
            )
        for shot in shots:
            vocal = shot.get("vocal_performance")
            if not isinstance(vocal, dict) or vocal.get("mode") not in ("none", "singing"):
                fail(
                    "invalid_production_brief",
                    detail="every MV shot requires vocal_performance.mode set to none or singing",
                )
            if vocal["mode"] == "singing":
                required_vocal = ("subject_id", "speaker_id", "language", "lyrics")
                if (
                    any(not isinstance(vocal.get(key), str) or not vocal[key].strip() for key in required_vocal)
                    or re.fullmatch(r"Subject [1-9][0-9]*", vocal["subject_id"]) is None
                    or re.fullmatch(r"S[1-9][0-9]*", vocal["speaker_id"]) is None
                ):
                    fail(
                        "invalid_production_brief",
                        detail="singing vocal_performance requires subject_id like Subject 1, speaker_id like S1, source language, and exact un-translated lyrics",
                    )
        brief["source_audio_path"] = str(audio_path)
        brief["source_audio_sha256"] = file_hash(audio_path)


def command_define_brief(conn, session_id, brief_json):
    row = get_row(conn, session_id)
    if row is None:
        fail("workflow_not_started")
    if row["state"] != "brief":
        fail(
            "stage_locked",
            current_state=row["state"],
            required_state="brief",
            recovery="The production brief is locked once dependent stages begin; start a new session for a different treatment",
        )
    try:
        brief = json.loads(brief_json)
        exact_brief = json.loads(
            brief_json,
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=Decimal,
        )
    except (json.JSONDecodeError, ArithmeticError) as exc:
        fail("invalid_production_brief", detail=str(exc))
    if not isinstance(brief, dict):
        fail("invalid_production_brief", detail="brief must be an object")
    validate_production_brief(brief, exact_brief)
    if brief["project_type"] == "mv":
        exact_brief["source_audio_path"] = brief["source_audio_path"]
        exact_brief["source_audio_sha256"] = brief["source_audio_sha256"]
    try:
        canonical = canonical_decimal_json(exact_brief)
    except (TypeError, ValueError) as exc:
        fail("invalid_production_brief", detail=str(exc))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    previous = conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM production_treatments WHERE session_id = ?",
        (session_id,),
    ).fetchone()[0]
    version = previous + 1
    timestamp = now()
    conn.execute(
        "INSERT INTO production_treatments(session_id,version,brief_json,sha256,created_at) "
        "VALUES(?,?,?,?,?)",
        (session_id, version, canonical, digest, timestamp),
    )
    transition(
        conn, session_id, "define_brief", row["state"], "treatment_approved",
        {"version": version, "sha256": digest},
    )
    conn.commit()
    result = {
        "session_id": session_id, "state": "treatment_approved",
        "version": version, "sha256": digest,
    }
    if brief["project_type"] == "mv":
        result["source_audio_sha256"] = brief["source_audio_sha256"]
    return result


def validate_cut_plan(plan, brief, exact_plan, exact_target):
    if not isinstance(plan, dict) or not isinstance(plan.get("cuts"), list) or not plan["cuts"]:
        fail("invalid_cut_plan", detail="cut plan requires a non-empty cuts array")
    cuts = plan["cuts"]
    start_fields = (
        "scene", "characters", "objects", "character_pose", "character_expression",
        "composition", "camera", "lighting",
    )
    action_fields = (
        "camera_movement", "scene_changes", "character_actions", "facial_changes",
        "body_motion", "temporal_progression", "end_state", "sound",
    )
    exact_cuts = exact_plan["cuts"]
    cut_ids = []
    segment_ids = []
    for cut, exact_cut in zip(cuts, exact_cuts):
        if (
            not isinstance(cut, dict)
            or not isinstance(cut.get("id"), str) or not cut["id"].strip()
            or not isinstance(cut.get("scene_id"), str) or not cut["scene_id"].strip()
        ):
            fail("invalid_cut_plan", detail="every cut requires non-empty id and scene_id")
        duration = cut.get("duration_seconds")
        if (
            not isinstance(duration, (int, float)) or isinstance(duration, bool)
            or not math.isfinite(duration)
        ):
            fail("invalid_cut_plan", detail="every editorial cut duration must be a finite number within 1–15 seconds")
        duration_decimal = exact_cut["duration_seconds"]
        if not duration_decimal.is_finite():
            fail("invalid_cut_plan", detail="every editorial cut duration must be a finite number within 1–15 seconds")
        if duration_decimal < Decimal("1") or duration_decimal > Decimal("15"):
            fail("invalid_cut_plan", detail="every editorial cut duration must be within 1–15 seconds")
        cut_ids.append(cut["id"])
        start_frame = cut.get("start_frame")
        if (
            not isinstance(start_frame, dict)
            or any(not isinstance(start_frame.get(key), str) or not start_frame[key].strip()
                   for key in start_fields)
        ):
            fail(
                "invalid_cut_plan",
                detail="every start_frame requires scene, characters, objects, character_pose, character_expression, composition, camera, and lighting",
            )
        action = cut.get("action")
        if (
            not isinstance(action, dict)
            or any(not isinstance(action.get(key), str) or not action[key].strip()
                   for key in action_fields)
        ):
            fail(
                "invalid_cut_plan",
                detail="every action requires camera_movement, scene_changes, character_actions, facial_changes, body_motion, temporal_progression, end_state, and sound",
            )
        segments = cut.get("generation_segments")
        if not isinstance(segments, list) or not segments:
            fail("invalid_cut_plan", detail="every cut requires at least one generation segment")
        exact_segments = exact_cut["generation_segments"]
        expected_offset = Decimal("0")
        for index, (segment, exact_segment) in enumerate(zip(segments, exact_segments)):
            if (
                not isinstance(segment, dict)
                or not isinstance(segment.get("id"), str) or not segment["id"].strip()
                or not isinstance(segment.get("action_slice"), str) or not segment["action_slice"].strip()
                or not isinstance(segment.get("end_state"), str) or not segment["end_state"].strip()
            ):
                fail("invalid_cut_plan", detail="every generation segment requires id, action_slice, and end_state")
            segment_duration = segment.get("duration_seconds")
            offset = segment.get("start_offset_seconds")
            exact_segment_duration = exact_segment["duration_seconds"]
            exact_offset = exact_segment["start_offset_seconds"]
            if (
                not isinstance(segment_duration, (int, float)) or isinstance(segment_duration, bool)
                or not math.isfinite(segment_duration)
                or not exact_segment_duration.is_finite()
                or exact_segment_duration <= Decimal("0")
                or exact_segment_duration > Decimal("5.2")
            ):
                fail("invalid_cut_plan", detail="every local H3 generation segment must be a finite positive number at most 5.2 seconds")
            if (
                not isinstance(offset, (int, float)) or isinstance(offset, bool)
                or not math.isfinite(offset)
                or not exact_offset.is_finite()
                or exact_offset != expected_offset
            ):
                fail("invalid_cut_plan", detail="generation segments must be exactly contiguous from start_offset_seconds=0")
            expected_continuation = "storyboard" if index == 0 else "previous_last_frame"
            if segment.get("continuation") != expected_continuation:
                fail(
                    "invalid_cut_plan",
                    detail="the first generation segment must use continuation=storyboard and later segments previous_last_frame",
                )
            if brief.get("project_type") == "mv" and (
                not isinstance(segment.get("audio_start_seconds"), (int, float))
                or isinstance(segment.get("audio_start_seconds"), bool)
                or not math.isfinite(segment["audio_start_seconds"])
                or not exact_segment["audio_start_seconds"].is_finite()
                or exact_segment["audio_start_seconds"] < Decimal("0")
            ):
                fail("invalid_cut_plan", detail="every MV generation segment requires audio_start_seconds")
            segment_ids.append(segment["id"])
            expected_offset += exact_segment_duration
        if expected_offset != duration_decimal:
            fail("invalid_cut_plan", detail="generation segment durations must sum exactly to their editorial cut duration")
    if len(cut_ids) != len(set(cut_ids)):
        fail("invalid_cut_plan", detail="cut ids must be unique")
    if len(segment_ids) != len(set(segment_ids)):
        fail("invalid_cut_plan", detail="generation segment ids must be unique")
    cut_total = sum((cut["duration_seconds"] for cut in exact_cuts), Decimal("0"))
    if cut_total != exact_target:
        fail("invalid_cut_plan", detail="editorial cut durations must sum exactly to target_duration_seconds")


def command_define_cut_plan(conn, session_id, cut_plan_json):
    row = get_row(conn, session_id)
    if row is None:
        fail("workflow_not_started")
    if row["state"] not in ("character_sheet_approved", "cut_plan_review_failed"):
        fail(
            "stage_locked",
            current_state=row["state"],
            required_state="character_sheet_approved",
            recovery="Call video_workflow status and execute only workflow_guidance.next_tool",
        )
    try:
        plan = json.loads(cut_plan_json)
        exact_plan = json.loads(
            cut_plan_json,
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=Decimal,
        )
    except (json.JSONDecodeError, ArithmeticError) as exc:
        fail("invalid_cut_plan", detail=str(exc))
    brief_row = conn.execute(
        "SELECT brief_json FROM production_treatments WHERE session_id = ? ORDER BY version DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    if brief_row is None:
        fail("invalid_cut_plan", detail="a locked production brief is required")
    brief = json.loads(brief_row["brief_json"])
    exact_brief = json.loads(
        brief_row["brief_json"],
        parse_float=Decimal,
        parse_int=Decimal,
        parse_constant=Decimal,
    )
    validate_cut_plan(plan, brief, exact_plan, exact_brief["target_duration_seconds"])
    try:
        canonical = canonical_decimal_json(exact_plan)
    except (TypeError, ValueError) as exc:
        fail("invalid_cut_plan", detail=str(exc))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    previous = conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM cut_plans WHERE session_id = ?",
        (session_id,),
    ).fetchone()[0]
    version = previous + 1
    timestamp = now()
    exact_total = sum(
        (cut["duration_seconds"] for cut in exact_plan["cuts"]),
        Decimal("0"),
    )
    exact_total_text = canonical_decimal_json(exact_total)
    total = float(exact_total)
    conn.execute(
        "INSERT INTO cut_plans(session_id,version,plan_json,sha256,cut_count,total_duration_seconds,total_duration_text,created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (session_id, version, canonical, digest, len(plan["cuts"]), total, exact_total_text, timestamp),
    )
    for ordinal, cut in enumerate(plan["cuts"], start=1):
        conn.execute(
            "INSERT INTO cut_plan_items(session_id,plan_version,cut_id,ordinal,duration_seconds,duration_seconds_text,scene_id,start_frame_json,action_json,generation_segments_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                session_id, version, cut["id"], ordinal, cut["duration_seconds"],
                canonical_decimal_json(exact_plan["cuts"][ordinal - 1]["duration_seconds"]), cut["scene_id"],
                json.dumps(cut["start_frame"], ensure_ascii=False, sort_keys=True),
                json.dumps(cut["action"], ensure_ascii=False, sort_keys=True),
                canonical_decimal_json(exact_plan["cuts"][ordinal - 1]["generation_segments"]),
            ),
        )
    transition(
        conn, session_id, "define_cut_plan", row["state"], "cut_plan_pending_review",
        {"version": version, "sha256": digest, "cut_count": len(plan["cuts"]), "total_duration_seconds_exact": exact_total_text},
    )
    conn.commit()
    return {
        "session_id": session_id,
        "state": "cut_plan_pending_review",
        "version": version,
        "sha256": digest,
        "cut_count": len(plan["cuts"]),
        "total_duration_seconds": total,
        "total_duration_seconds_exact": exact_total_text,
    }


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command_submit(conn, session_id, stage, artifacts):
    row = get_row(conn, session_id)
    if row is None:
        fail("workflow_not_started")
    state = row["state"]
    required_states = set(REQUIRED_STATE[stage])
    if state not in required_states:
        fail(
            "stage_locked",
            current_state=state,
            required_state=LOCK_MESSAGE_STATE[stage],
            recovery="Call video_workflow status and execute only workflow_guidance.next_tool",
        )

    artifact_keys = [None] * len(artifacts)
    if stage == "storyboards":
        plan_row = conn.execute(
            "SELECT version FROM cut_plans WHERE session_id = ? ORDER BY version DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if plan_row is None:
            fail("stage_locked", current_state=state, required_state="cut_plan_approved")
        cut_ids = [
            item["cut_id"] for item in conn.execute(
                "SELECT cut_id FROM cut_plan_items WHERE session_id = ? AND plan_version = ? ORDER BY ordinal",
                (session_id, plan_row["version"]),
            ).fetchall()
        ]
        if len(artifacts) != len(cut_ids):
            fail(
                "incomplete_storyboard_coverage",
                expected_cut_ids=cut_ids,
                received_artifact_count=len(artifacts),
                detail="submit exactly one storyboard artifact per editorial cut in cut-plan order",
            )
        artifact_keys = cut_ids
        resolved_storyboards = [str(Path(path).expanduser().resolve()) for path in artifacts]
        if len(set(resolved_storyboards)) != len(resolved_storyboards):
            fail(
                "duplicate_storyboard_artifact",
                detail="each editorial cut requires a distinct storyboard file",
            )

    artifact_rows = []
    for raw_path, artifact_key in zip(artifacts, artifact_keys):
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file():
            fail("artifact_not_found", path=str(path))
        artifact_rows.append({
            "path": str(path),
            "sha256": file_hash(path),
            **({"cut_id": artifact_key} if artifact_key is not None else {}),
        })
    if stage == "storyboards":
        storyboard_hashes = [artifact["sha256"] for artifact in artifact_rows]
        if len(set(storyboard_hashes)) != len(storyboard_hashes):
            fail(
                "duplicate_storyboard_content",
                detail="each editorial cut requires visually distinct storyboard file content; duplicate SHA256 values are not allowed",
            )

    timestamp = now()
    conn.execute("DELETE FROM stage_artifacts WHERE session_id = ? AND stage = ?", (session_id, stage))
    for artifact in artifact_rows:
        conn.execute(
            "INSERT INTO stage_artifacts(session_id,stage,artifact_key,path,sha256,submitted_at) VALUES(?,?,?,?,?,?)",
            (session_id, stage, artifact.get("cut_id"), artifact["path"], artifact["sha256"], timestamp),
        )
    new_state = f"{stage}_pending_review"
    transition(conn, session_id, "submit", state, new_state, {"artifacts": artifact_rows})
    conn.commit()
    return {"session_id": session_id, "state": new_state, "stage": stage, "artifacts": artifact_rows}


def command_review(conn, session_id, stage, verdict, checklist_json, reason, reviewer):
    row = get_row(conn, session_id)
    if row is None:
        fail("workflow_not_started")
    expected = f"{stage}_pending_review"
    if row["state"] != expected:
        fail("review_not_pending", current_state=row["state"], required_state=expected)
    try:
        checklist = json.loads(checklist_json)
    except json.JSONDecodeError as exc:
        fail("invalid_checklist_json", detail=str(exc))
    if not isinstance(checklist, dict):
        fail("invalid_checklist", detail="checklist must be an object")

    if verdict == "pass":
        if stage == "storyboards":
            plan_row = conn.execute(
                "SELECT version FROM cut_plans WHERE session_id = ? ORDER BY version DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            expected_cut_ids = [
                item["cut_id"] for item in conn.execute(
                    "SELECT cut_id FROM cut_plan_items WHERE session_id = ? AND plan_version = ? ORDER BY ordinal",
                    (session_id, plan_row["version"]),
                ).fetchall()
            ] if plan_row else []
            submitted_cut_ids = [
                item["artifact_key"] for item in conn.execute(
                    "SELECT artifact_key FROM stage_artifacts WHERE session_id = ? AND stage = 'storyboards' ORDER BY id",
                    (session_id,),
                ).fetchall()
            ]
            if submitted_cut_ids != expected_cut_ids:
                fail(
                    "incomplete_storyboard_coverage",
                    expected_cut_ids=expected_cut_ids,
                    submitted_cut_ids=submitted_cut_ids,
                    detail="storyboard review requires exactly one persisted artifact for every editorial cut",
                )
        brief = latest_production_brief(conn, session_id)
        required_fields = required_checklist(brief, stage)
        missing = [field for field in required_fields if checklist.get(field) is not True]
        if missing:
            fail(
                "incomplete_checklist",
                missing=missing,
                expected_checklist={field: True for field in required_fields},
                type_warning="Every checklist value must be JSON boolean true; never use a number or string",
                recovery="Visually inspect the submitted artifacts. Record fail if any check is false; otherwise retry once with the exact boolean schema",
            )
        if stage == "storyboards":
            evidence_missing = []
            pairwise = checklist.get("pairwise_evidence")
            if not isinstance(pairwise, list) or not pairwise or not all(
                isinstance(item, str) and item.strip() for item in pairwise
            ):
                evidence_missing.append("pairwise_evidence")
            sequence = checklist.get("sequence_style_evidence")
            if not isinstance(sequence, str) or not sequence.strip():
                evidence_missing.append("sequence_style_evidence")
            if evidence_missing:
                fail(
                    "missing_review_evidence",
                    missing=evidence_missing,
                    expected_evidence={
                        "pairwise_evidence": [
                            "For every adjacent shot pair, name the largest visible difference and map it to the Shot Manifest"
                        ],
                        "sequence_style_evidence":
                            "Name the strongest sequence-wide style outlier, or explain why none exists",
                    },
                    recovery="Do the difference-first visual comparison; do not search implementation code",
                )
        new_state = f"{stage}_approved"
    else:
        new_state = f"{stage}_review_failed"

    timestamp = now()
    conn.execute(
        "INSERT INTO visual_reviews(session_id,stage,verdict,checklist_json,reason,reviewer,reviewed_at) VALUES(?,?,?,?,?,?,?)",
        (session_id, stage, verdict, json.dumps(checklist, sort_keys=True), reason, reviewer, timestamp),
    )
    transition(
        conn, session_id, "review", row["state"], new_state,
        {"stage": stage, "verdict": verdict, "checklist": checklist, "reason": reason, "reviewer": reviewer},
    )
    conn.commit()
    return {"session_id": session_id, "state": new_state, "stage": stage, "verdict": verdict}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--session", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start")
    sub.add_parser("status")
    define_brief = sub.add_parser("define-brief")
    define_brief.add_argument("--brief-json", required=True)
    define_cut_plan = sub.add_parser("define-cut-plan")
    define_cut_plan.add_argument("--cut-plan-json", required=True)
    submit = sub.add_parser("submit")
    submit.add_argument("--stage", choices=ARTIFACT_STAGES, required=True)
    submit.add_argument("--artifact", action="append", required=True)
    review = sub.add_parser("review")
    review.add_argument("--stage", choices=REVIEW_STAGES, required=True)
    review.add_argument("--verdict", choices=("pass", "fail"), required=True)
    review.add_argument("--checklist-json", required=True)
    review.add_argument("--reason", required=True)
    review.add_argument("--reviewer")
    return parser.parse_args()


def main():
    args = parse_args()
    conn = connect(args.db)
    try:
        if args.command == "start":
            result = command_start(conn, args.session)
        elif args.command == "status":
            result = command_status(conn, args.session)
        elif args.command == "define-brief":
            result = command_define_brief(conn, args.session, args.brief_json)
        elif args.command == "define-cut-plan":
            result = command_define_cut_plan(conn, args.session, args.cut_plan_json)
        elif args.command == "submit":
            result = command_submit(conn, args.session, args.stage, args.artifact)
        else:
            result = command_review(
                conn, args.session, args.stage, args.verdict,
                args.checklist_json, args.reason, args.reviewer,
            )
        emit(result)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
