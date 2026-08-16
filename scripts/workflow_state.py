#!/usr/bin/env python3
"""SQLite-backed workflow state for the Pi create-video guard."""

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

STAGES = ("character_sheet", "storyboards", "clips", "final")
REQUIRED_STATE = {
    "character_sheet": {"treatment_approved", "character_sheet_review_failed"},
    "storyboards": {"character_sheet_approved", "storyboards_review_failed"},
    "clips": {"storyboards_approved", "clips_review_failed"},
    "final": {"clips_approved", "final_review_failed"},
}
LOCK_MESSAGE_STATE = {
    "character_sheet": "treatment_approved",
    "storyboards": "character_sheet_approved",
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


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit(payload, *, error=False):
    stream = sys.stderr if error else sys.stdout
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=stream)


def fail(code: str, **details):
    emit({"error": code, **details}, error=True)
    raise SystemExit(2)


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
        CREATE TABLE IF NOT EXISTS stage_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            stage TEXT NOT NULL,
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
        result.update({
            "treatment_version": treatment["version"],
            "treatment_sha256": treatment["sha256"],
            "project_type": brief["project_type"],
            "target_duration_seconds": brief["target_duration_seconds"],
            "shot_count": len(brief["shot_manifest"]),
            "production_brief": brief,
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


def validate_production_brief(brief):
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
    if not isinstance(target, (int, float)) or isinstance(target, bool) or target <= 0:
        fail("invalid_production_brief", detail="target_duration_seconds must be positive")
    shots = brief["shot_manifest"]
    if not isinstance(shots, list) or not shots:
        fail("invalid_production_brief", detail="shot_manifest must contain at least one shot")
    if any(
        not isinstance(shot, dict)
        or not isinstance(shot.get("id"), str) or not shot["id"].strip()
        or not isinstance(shot.get("beat"), str) or not shot["beat"].strip()
        or not isinstance(shot.get("duration_seconds"), (int, float))
        or isinstance(shot.get("duration_seconds"), bool)
        or shot["duration_seconds"] <= 0
        for shot in shots
    ):
        fail("invalid_production_brief", detail="each shot needs id, beat, and positive duration_seconds")
    shot_ids = [shot["id"] for shot in shots]
    if len(set(shot_ids)) != len(shot_ids):
        fail("invalid_production_brief", detail="shot ids must be unique")
    if abs(sum(shot["duration_seconds"] for shot in shots) - target) > 0.05:
        fail("invalid_production_brief", detail="shot durations must sum to target_duration_seconds")
    if any(shot["duration_seconds"] > 5.2 for shot in shots):
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
            or shot["audio_start_seconds"] < 0
            or shot["duration_seconds"] < 2
            or shot["duration_seconds"] > 5.2
            for shot in shots
        ):
            fail(
                "invalid_production_brief",
                detail="MV shots require audio_start_seconds and local 2–5.2 second durations",
            )
        brief["source_audio_path"] = str(audio_path)
        brief["source_audio_sha256"] = file_hash(audio_path)


def command_define_brief(conn, session_id, brief_json):
    row = get_row(conn, session_id)
    if row is None:
        fail("workflow_not_started")
    try:
        brief = json.loads(brief_json)
    except json.JSONDecodeError as exc:
        fail("invalid_production_brief", detail=str(exc))
    if not isinstance(brief, dict):
        fail("invalid_production_brief", detail="brief must be an object")
    validate_production_brief(brief)
    canonical = json.dumps(brief, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
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
    brief = latest_production_brief(conn, session_id)
    if stage == "storyboards" and brief and storyboard_policy(brief)["mode"] == "direct":
        fail(
            "storyboards_not_required",
            current_state=state,
            recovery="Generate H3 segments directly and seed same-scene continuations from the previous clip's actual last frame",
        )
    if stage == "clips" and brief and storyboard_policy(brief)["mode"] == "direct":
        required_states.add("character_sheet_approved")
    if state not in required_states:
        fail(
            "stage_locked",
            current_state=state,
            required_state=LOCK_MESSAGE_STATE[stage],
            recovery="Call video_workflow status and execute only workflow_guidance.next_tool",
        )

    artifact_rows = []
    for raw_path in artifacts:
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file():
            fail("artifact_not_found", path=str(path))
        artifact_rows.append({"path": str(path), "sha256": file_hash(path)})

    timestamp = now()
    conn.execute("DELETE FROM stage_artifacts WHERE session_id = ? AND stage = ?", (session_id, stage))
    for artifact in artifact_rows:
        conn.execute(
            "INSERT INTO stage_artifacts(session_id,stage,path,sha256,submitted_at) VALUES(?,?,?,?,?)",
            (session_id, stage, artifact["path"], artifact["sha256"], timestamp),
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
        missing = [field for field in CHECKLISTS[stage] if checklist.get(field) is not True]
        if missing:
            fail(
                "incomplete_checklist",
                missing=missing,
                expected_checklist={field: True for field in CHECKLISTS[stage]},
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
    submit = sub.add_parser("submit")
    submit.add_argument("--stage", choices=STAGES, required=True)
    submit.add_argument("--artifact", action="append", required=True)
    review = sub.add_parser("review")
    review.add_argument("--stage", choices=STAGES, required=True)
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
