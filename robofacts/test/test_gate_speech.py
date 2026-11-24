"""The spoken sentence of a failed gate check names what failed (lab day 2026-11-09)."""
from robofacts.gate_speech import sentence


def test_rigidbody_continuity_names_the_body():
    detail = "rigid body 'spot' present in only 0/380 frames (< 95 %) - wrong name, or the launch spot is a tracking dead zone"
    assert sentence({"check": "rigidbody_continuity", "detail": detail}) == "Mocap does not track the rigid body spot."


def test_rigidbody_continuity_without_a_name_stays_generic_but_not_about_a_drone():
    detail = "/poses continuity: only 0 then 0 frames in 5s across TWO attempts"
    assert sentence({"check": "rigidbody_continuity", "detail": detail}) == "Mocap does not track the rigid body."


def test_rigidbody_continuity_speaks_underscores_as_spaces():
    detail = "rigid body 'cfb1_bare' present in only 3/300 frames"
    assert sentence({"check": "rigidbody_continuity", "detail": detail}) == "Mocap does not track the rigid body cfb1 bare."
