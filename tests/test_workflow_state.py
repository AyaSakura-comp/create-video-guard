import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "workflow_state.py"


def run_cli(db: Path, session: str, *args: str, ok: bool = True):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(db), "--session", session, *args],
        text=True,
        capture_output=True,
    )
    if ok:
        if proc.returncode != 0:
            raise AssertionError(proc.stderr)
        return json.loads(proc.stdout)
    if proc.returncode == 0:
        raise AssertionError("command unexpectedly succeeded")
    return json.loads(proc.stderr)


def production_brief(user_request: str = "A dancer crosses a moonlit stage") -> str:
    return json.dumps({
        "user_request": user_request,
        "project_type": "narrative",
        "target_duration_seconds": 10,
        "explicit_requirements": ["dancer", "moonlit stage"],
        "agent_assumptions": [{
            "assumption": "No dialogue",
            "basis": "The request does not ask for speech",
            "confidence": "medium",
        }],
        "creative_choices": ["Three-shot chase structure"],
        "treatment": {
            "logline": "A dancer completes a difficult performance.",
            "emotional_arc": "uncertain to triumphant",
        },
        "shot_manifest": [
            {"id": "S01", "duration_seconds": 4, "beat": "establish stage", "scene_id": "stage", "continuation": "storyboard", "camera": "wide eye-level push-in", "action": "dancer enters", "dialogue": "none", "sound": "auditorium ambience"},
            {"id": "S02", "duration_seconds": 3, "beat": "difficult turn", "scene_id": "stage", "continuation": "storyboard", "camera": "medium side tracking", "action": "dancer turns", "dialogue": "none", "sound": "footfalls"},
            {"id": "S03", "duration_seconds": 3, "beat": "final pose", "scene_id": "stage", "continuation": "storyboard", "camera": "low-angle slow push", "action": "dancer lands", "dialogue": "none", "sound": "applause"},
        ],
        "continuity_bible": {
            "screen_direction": "left-to-right",
            "storyboard_policy": {
                "mode": "full",
                "reason": "test fixture exercises the full storyboard gate",
                "storyboard_shot_ids": ["S01", "S02", "S03"],
            },
            "style_bible": {
                "positive_prompt_prefix": "masterpiece, best quality, score_7, safe, anime screencap, crisp clean linework, flat two-step cel shading, restrained blue-violet palette",
                "negative_prompt": "worst quality, low quality, score_1, score_2, score_3, artist name, blurry, jpeg artifacts, chromatic aberration, photorealistic, 3d render",
                "line_grammar": "crisp uniform ink lines",
                "cel_shading": "flat two-step cel shading",
                "palette": "restrained blue-violet with warm skin tones",
                "background_rendering": "painted anime background with matching line density",
                "contrast": "medium-high",
                "color_temperature": "cool moonlight",
            },
            "generation_lock": {
                "checkpoint": "anima_baseV10.safetensors",
                "sampler": "er_sde",
                "steps": 30,
                "cfg": 4.5,
                "resolution": "864x480",
            },
        },
        "audio_plan": {"dialogue": "none", "ambience": "auditorium"},
    })


def define_brief(db: Path, session: str):
    return run_cli(db, session, "define-brief", "--brief-json", production_brief())


def pass_checklist(stage: str) -> str:
    fields = {
        "character_sheet": [
            "exact_character_count", "full_body_visible", "identity_features_consistent",
            "pure_white_background", "no_duplicates_or_extras",
            "single_view_per_character", "no_insets_labels_or_swatches",
            "anatomy_uncropped",
        ],
        "storyboards": [
            "all_planned_shots_present", "identity_consistent",
            "composition_matches_shot_map", "line_weight_consistent",
            "cel_shading_consistent", "palette_temperature_consistent",
            "background_rendering_consistent", "scene_geography_consistent",
            "screen_direction_eyelines_consistent", "props_costume_hands_consistent",
            "adjacent_cuts_compatible", "style_outliers_absent",
        ],
        "clips": [
            "identity_consistent", "motion_matches_intent", "no_visual_artifacts",
            "continuity_preserved",
        ],
        "final": [
            "joins_clean", "audiovisual_sync", "style_consistent", "exact_duration",
        ],
    }
    checklist = {key: True for key in fields[stage]}
    if stage == "storyboards":
        checklist["pairwise_evidence"] = ["shot01->shot02: compared at equal scale"]
        checklist["sequence_style_evidence"] = "Global strip compared against the locked style key"
    return json.dumps(checklist)


class WorkflowStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.temp.name)
        self.db = self.tmp_path / "state.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def make_artifact(self, name: str = "frame.png") -> Path:
        artifact = self.tmp_path / name
        artifact.write_bytes(b"visual evidence")
        return artifact

    def test_start_creates_brief_workflow(self):
        result = run_cli(self.db, "session-a", "start")
        self.assertEqual(result["session_id"], "session-a")
        self.assertEqual(result["state"], "brief")

    def test_same_scene_direct_plan_allows_clip_submission_after_character_approval(self):
        run_cli(self.db, "session-direct", "start")
        brief = json.loads(production_brief())
        brief["continuity_bible"]["storyboard_policy"] = {
            "mode": "direct",
            "reason": "all segments stay on the same moonlit stage",
            "storyboard_shot_ids": [],
        }
        for index, shot in enumerate(brief["shot_manifest"]):
            shot["continuation"] = "none" if index == 0 else "previous_last_frame"
        run_cli(
            self.db, "session-direct", "define-brief",
            "--brief-json", json.dumps(brief),
        )
        character = self.make_artifact("direct-character.png")
        run_cli(
            self.db, "session-direct", "submit", "--stage", "character_sheet",
            "--artifact", str(character),
        )
        run_cli(
            self.db, "session-direct", "review", "--stage", "character_sheet",
            "--verdict", "pass", "--checklist-json", pass_checklist("character_sheet"),
            "--reason", "valid", "--reviewer", "test",
        )
        clip_qc = self.make_artifact("direct-clips-qc.png")
        submitted = run_cli(
            self.db, "session-direct", "submit", "--stage", "clips",
            "--artifact", str(clip_qc),
        )
        self.assertEqual(submitted["state"], "clips_pending_review")

    def test_same_scene_direct_plan_blocks_unnecessary_storyboard_submission(self):
        run_cli(self.db, "session-no-storyboards", "start")
        brief = json.loads(production_brief())
        brief["continuity_bible"]["storyboard_policy"] = {
            "mode": "direct", "reason": "one unchanged scene", "storyboard_shot_ids": [],
        }
        for index, shot in enumerate(brief["shot_manifest"]):
            shot["continuation"] = "none" if index == 0 else "previous_last_frame"
        run_cli(
            self.db, "session-no-storyboards", "define-brief",
            "--brief-json", json.dumps(brief),
        )
        character = self.make_artifact("no-storyboards-character.png")
        run_cli(
            self.db, "session-no-storyboards", "submit", "--stage", "character_sheet",
            "--artifact", str(character),
        )
        run_cli(
            self.db, "session-no-storyboards", "review", "--stage", "character_sheet",
            "--verdict", "pass", "--checklist-json", pass_checklist("character_sheet"),
            "--reason", "valid", "--reviewer", "test",
        )
        storyboard = self.make_artifact("unnecessary-storyboard.png")
        error = run_cli(
            self.db, "session-no-storyboards", "submit", "--stage", "storyboards",
            "--artifact", str(storyboard), ok=False,
        )
        self.assertEqual(error["error"], "storyboards_not_required")

    def test_same_scene_direct_plan_rejects_storyboard_ids(self):
        run_cli(self.db, "session-invalid-direct", "start")
        brief = json.loads(production_brief())
        brief["continuity_bible"]["storyboard_policy"] = {
            "mode": "direct",
            "reason": "same scene",
            "storyboard_shot_ids": ["S02"],
        }
        error = run_cli(
            self.db, "session-invalid-direct", "define-brief",
            "--brief-json", json.dumps(brief), ok=False,
        )
        self.assertEqual(error["error"], "invalid_production_brief")
        self.assertIn("direct", error["detail"])

    def test_segment_longer_than_five_point_two_seconds_must_be_split(self):
        run_cli(self.db, "session-long-segment", "start")
        brief = json.loads(production_brief())
        brief["target_duration_seconds"] = 10
        brief["shot_manifest"] = [{
            "id": "S01", "duration_seconds": 10, "beat": "one long move",
            "scene_id": "stage", "continuation": "none", "camera": "slow orbit",
            "action": "dancer circles the stage", "dialogue": "none", "sound": "music",
        }]
        brief["continuity_bible"]["storyboard_policy"] = {
            "mode": "direct", "reason": "same scene", "storyboard_shot_ids": [],
        }
        error = run_cli(
            self.db, "session-long-segment", "define-brief",
            "--brief-json", json.dumps(brief), ok=False,
        )
        self.assertEqual(error["error"], "invalid_production_brief")
        self.assertIn("5.2", error["detail"])
        self.assertIn("previous_last_frame", error["detail"])

    def test_character_sheet_requires_agent_completed_production_brief(self):
        artifact = self.make_artifact()
        run_cli(self.db, "session-a", "start")
        error = run_cli(
            self.db, "session-a", "submit", "--stage", "character_sheet",
            "--artifact", str(artifact), ok=False,
        )
        self.assertEqual(error["error"], "stage_locked")
        self.assertEqual(error["required_state"], "treatment_approved")
        self.assertIn("video_workflow status", error["recovery"])

    def test_agent_can_expand_vague_request_into_locked_production_brief(self):
        run_cli(self.db, "session-a", "start")
        result = define_brief(self.db, "session-a")
        self.assertEqual(result["state"], "treatment_approved")
        self.assertEqual(result["version"], 1)
        self.assertEqual(len(result["sha256"]), 64)
        status = run_cli(self.db, "session-a", "status")
        self.assertEqual(status["treatment_version"], 1)
        self.assertEqual(status["project_type"], "narrative")
        self.assertEqual(status["target_duration_seconds"], 10)
        self.assertEqual(status["shot_count"], 3)
        self.assertEqual(status["production_brief"]["user_request"], "A dancer crosses a moonlit stage")
        self.assertEqual(len(status["production_brief"]["shot_manifest"]), 3)

    def test_production_brief_requires_locked_style_bible_and_generation_settings(self):
        run_cli(self.db, "session-a", "start")
        brief = json.loads(production_brief())
        del brief["continuity_bible"]["style_bible"]["positive_prompt_prefix"]
        error = run_cli(
            self.db, "session-a", "define-brief", "--brief-json", json.dumps(brief), ok=False,
        )
        self.assertEqual(error["error"], "invalid_production_brief")
        self.assertIn("style_bible", error["detail"])

    def test_production_brief_rejects_unattributed_assumptions(self):
        run_cli(self.db, "session-a", "start")
        brief = json.loads(production_brief())
        brief["agent_assumptions"] = ["No dialogue"]
        error = run_cli(
            self.db, "session-a", "define-brief", "--brief-json", json.dumps(brief), ok=False,
        )
        self.assertEqual(error["error"], "invalid_production_brief")
        self.assertIn("agent_assumptions", error["detail"])

    def test_production_brief_rejects_shot_timing_that_does_not_match_target(self):
        run_cli(self.db, "session-a", "start")
        brief = json.loads(production_brief())
        brief["shot_manifest"][2]["duration_seconds"] = 1
        error = run_cli(
            self.db, "session-a", "define-brief", "--brief-json", json.dumps(brief), ok=False,
        )
        self.assertEqual(error["error"], "invalid_production_brief")
        self.assertIn("durations", error["detail"])

    def test_mv_brief_requires_and_hashes_source_audio(self):
        run_cli(self.db, "session-mv", "start")
        audio = self.tmp_path / "song.mp3"
        audio.write_bytes(b"source song")
        brief = json.loads(production_brief("Make a music video for this song"))
        brief["project_type"] = "mv"
        brief["source_audio_path"] = str(audio)
        for shot, start in zip(brief["shot_manifest"], (0, 4, 7)):
            shot["audio_start_seconds"] = start
        result = run_cli(
            self.db, "session-mv", "define-brief", "--brief-json", json.dumps(brief),
        )
        self.assertEqual(result["state"], "treatment_approved")
        self.assertEqual(len(result["source_audio_sha256"]), 64)

    def test_mv_brief_rejects_missing_source_audio(self):
        run_cli(self.db, "session-mv", "start")
        brief = json.loads(production_brief("Make a music video"))
        brief["project_type"] = "mv"
        error = run_cli(
            self.db, "session-mv", "define-brief", "--brief-json", json.dumps(brief), ok=False,
        )
        self.assertEqual(error["error"], "invalid_production_brief")
        self.assertIn("source_audio_path", error["detail"])

    def test_cannot_skip_character_sheet_review(self):
        artifact = self.make_artifact()
        run_cli(self.db, "session-a", "start")
        define_brief(self.db, "session-a")
        error = run_cli(
            self.db, "session-a", "submit", "--stage", "storyboards",
            "--artifact", str(artifact), ok=False,
        )
        self.assertEqual(error["error"], "stage_locked")
        self.assertEqual(error["required_state"], "character_sheet_approved")

    def test_passing_complete_review_unlocks_next_stage_and_hashes_artifact(self):
        artifact = self.make_artifact()
        run_cli(self.db, "session-a", "start")
        define_brief(self.db, "session-a")
        submitted = run_cli(
            self.db, "session-a", "submit", "--stage", "character_sheet",
            "--artifact", str(artifact),
        )
        self.assertEqual(submitted["state"], "character_sheet_pending_review")
        self.assertEqual(len(submitted["artifacts"][0]["sha256"]), 64)

        reviewed = run_cli(
            self.db, "session-a", "review", "--stage", "character_sheet",
            "--verdict", "pass", "--checklist-json", pass_checklist("character_sheet"),
            "--reason", "All required visual checks passed",
        )
        self.assertEqual(reviewed["state"], "character_sheet_approved")
        self.assertEqual(reviewed["verdict"], "pass")

    def test_storyboard_pass_requires_sequence_level_style_checks(self):
        character = self.make_artifact("character.png")
        storyboard = self.make_artifact("storyboard-contact.png")
        run_cli(self.db, "session-a", "start")
        define_brief(self.db, "session-a")
        run_cli(
            self.db, "session-a", "submit", "--stage", "character_sheet",
            "--artifact", str(character),
        )
        run_cli(
            self.db, "session-a", "review", "--stage", "character_sheet",
            "--verdict", "pass", "--checklist-json", pass_checklist("character_sheet"),
            "--reason", "Character sheet passes",
        )
        run_cli(
            self.db, "session-a", "submit", "--stage", "storyboards",
            "--artifact", str(storyboard),
        )
        checklist = json.loads(pass_checklist("storyboards"))
        del checklist["adjacent_cuts_compatible"]
        error = run_cli(
            self.db, "session-a", "review", "--stage", "storyboards",
            "--verdict", "pass", "--checklist-json", json.dumps(checklist),
            "--reason", "Pairwise review was omitted", ok=False,
        )
        self.assertEqual(error["error"], "incomplete_checklist")
        self.assertIn("adjacent_cuts_compatible", error["missing"])

    def test_storyboard_pass_requires_written_pairwise_and_global_evidence(self):
        character = self.make_artifact("character-evidence.png")
        storyboard = self.make_artifact("storyboard-evidence.png")
        run_cli(self.db, "session-evidence", "start")
        define_brief(self.db, "session-evidence")
        run_cli(
            self.db, "session-evidence", "submit", "--stage", "character_sheet",
            "--artifact", str(character),
        )
        run_cli(
            self.db, "session-evidence", "review", "--stage", "character_sheet",
            "--verdict", "pass", "--checklist-json", pass_checklist("character_sheet"),
            "--reason", "Character sheet passes",
        )
        run_cli(
            self.db, "session-evidence", "submit", "--stage", "storyboards",
            "--artifact", str(storyboard),
        )
        checklist = json.loads(pass_checklist("storyboards"))
        del checklist["pairwise_evidence"]
        error = run_cli(
            self.db, "session-evidence", "review", "--stage", "storyboards",
            "--verdict", "pass", "--checklist-json", json.dumps(checklist),
            "--reason", "Evidence omitted", ok=False,
        )
        self.assertEqual(error["error"], "missing_review_evidence")
        self.assertIn("pairwise_evidence", error["missing"])

    def test_failed_or_incomplete_review_remains_locked(self):
        artifact = self.make_artifact()
        run_cli(self.db, "session-a", "start")
        define_brief(self.db, "session-a")
        run_cli(
            self.db, "session-a", "submit", "--stage", "character_sheet",
            "--artifact", str(artifact),
        )
        incomplete = run_cli(
            self.db, "session-a", "review", "--stage", "character_sheet",
            "--verdict", "pass", "--checklist-json",
            json.dumps({"exact_character_count": True}),
            "--reason", "Not enough evidence", ok=False,
        )
        self.assertEqual(incomplete["error"], "incomplete_checklist")
        self.assertIs(incomplete["expected_checklist"]["exact_character_count"], True)
        self.assertIn("boolean true", incomplete["type_warning"])
        self.assertIn("single_view_per_character", incomplete["missing"])

        failed = run_cli(
            self.db, "session-a", "review", "--stage", "character_sheet",
            "--verdict", "fail", "--checklist-json",
            json.dumps({"exact_character_count": False}),
            "--reason", "Extra character",
        )
        self.assertEqual(failed["state"], "character_sheet_review_failed")


if __name__ == "__main__":
    unittest.main()
