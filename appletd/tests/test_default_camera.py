"""Which camera `(default)` means, on machines that are not the author's.

WHY THIS FILE EXISTS. The default was a name substring, "MacBook", chosen on a Mac whose
built-in camera is "MacBook Pro Camera". On a Mac where it is "FaceTime HD Camera" the
sidecar exited at startup and the Video Device In TOP was left with no device - no
tracking and no picture, with the panel saying Running. Reported from somebody else's
laptop; invisible on the one that wrote the rule. The rule is now the device TYPE first
and the names second, and it is pure over a device list so that other machines' lists
can be held against it here, without their hardware.

Ref: appletd/engine.py default_device, appletd/sidecar.py _print_cameras.
"""

from __future__ import annotations

from collections.abc import Sequence

import AVFoundation as AVF
import pytest

from appletd import engine, sidecar
from appletd.engine import EngineError, default_device, pick_device
from appletd.streams import DEFAULT_CAMERA_MARK, DEFAULT_CAMERA_NAMES

BUILT_IN = str(AVF.AVCaptureDeviceTypeBuiltInWideAngleCamera)
EXTERNAL = "AVCaptureDeviceTypeExternal"
CONTINUITY = "AVCaptureDeviceTypeContinuityCamera"


class FakeDevice:
    """Just the three selectors the engine reads off an AVCaptureDevice."""

    def __init__(self, name: str, kind: str = EXTERNAL) -> None:
        self._name = name
        self._kind = kind

    def localizedName(self) -> str:
        return self._name

    def deviceType(self) -> str:
        return self._kind

    def uniqueID(self) -> str:
        return self._name.replace(" ", "-").lower()


# The list from the report: the built-in camera is called "FaceTime HD Camera".
REPORTED = (FakeDevice("FaceTime HD Camera", BUILT_IN),
            FakeDevice("NDI Virtual Camera"),
            FakeDevice("Merlin Cray Camera"))
# The author's machine, with the built-in camera deliberately LAST.
AUTHORS = (FakeDevice("Camo Camera"),
           FakeDevice("OBS Virtual Camera"),
           FakeDevice("Omer's iPhone Camera", CONTINUITY),
           FakeDevice("NDI Virtual Camera"),
           FakeDevice("MacBook Pro Camera", BUILT_IN))
VIRTUAL_ONLY = (FakeDevice("OBS Virtual Camera"), FakeDevice("NDI Virtual Camera"))


def names(devices: Sequence[FakeDevice]) -> list[str]:
    return [device.localizedName() for device in devices]


def test_the_reported_machine_gets_its_built_in_camera() -> None:
    chosen = default_device(REPORTED)
    assert chosen is not None
    assert chosen.localizedName() == "FaceTime HD Camera"


def test_the_built_in_type_wins_wherever_it_sits_in_the_list() -> None:
    chosen = default_device(AUTHORS)
    assert chosen is not None
    assert chosen.localizedName() == "MacBook Pro Camera"


def test_without_a_built_in_type_the_names_decide_in_their_order() -> None:
    devices = (FakeDevice("OBS Virtual Camera"),
               FakeDevice("FaceTime HD Camera"),           # external, by type
               FakeDevice("MacBook Air Camera"))
    chosen = default_device(devices)
    assert chosen is not None
    assert chosen.localizedName() == "MacBook Air Camera"
    assert DEFAULT_CAMERA_NAMES.index("MacBook") < DEFAULT_CAMERA_NAMES.index("FaceTime")


def test_the_name_match_is_case_insensitive() -> None:
    assert default_device((FakeDevice("facetime hd camera"),)) is not None


def test_only_virtual_cameras_is_nothing_rather_than_a_guess() -> None:
    """Index 0 would be OBS, and black frames from OBS is the trap DESIGN.md 3 names."""
    assert default_device(VIRTUAL_ONLY) is None


def test_the_fallback_names_are_substrings_of_real_built_in_camera_names() -> None:
    real = ("MacBook Pro Camera", "FaceTime HD Camera")
    for name in DEFAULT_CAMERA_NAMES:
        assert any(name in real_name for real_name in real), name


def test_pick_device_with_no_name_uses_the_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "discover_devices", lambda: list(REPORTED))
    assert pick_device(None).localizedName() == "FaceTime HD Camera"


def test_pick_device_with_no_match_names_what_was_there(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "discover_devices", lambda: list(VIRTUAL_ONLY))
    with pytest.raises(EngineError) as caught:
        pick_device(None)
    message = str(caught.value)
    for name in DEFAULT_CAMERA_NAMES:
        assert name in message
    for available in names(VIRTUAL_ONLY):
        assert available in message


def test_pick_device_by_name_is_still_a_case_insensitive_substring(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "discover_devices", lambda: list(AUTHORS))
    assert pick_device("obs").localizedName() == "OBS Virtual Camera"
    with pytest.raises(EngineError, match="Camo Camera"):
        pick_device("Logitech")


def test_list_cameras_marks_exactly_the_default(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The third column is how TouchDesigner learns which device `(default)` means."""
    monkeypatch.setattr(engine, "discover_devices", lambda: list(REPORTED))
    assert sidecar._print_cameras() == 0
    rows = [line.split("\t") for line in capsys.readouterr().out.splitlines()]
    assert [row[0] for row in rows] == names(REPORTED)
    assert all(len(row) == 3 for row in rows)
    assert [row[0] for row in rows if row[2] == DEFAULT_CAMERA_MARK] == ["FaceTime HD Camera"]


def test_list_cameras_with_nothing_to_mark_marks_nothing(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(engine, "discover_devices", lambda: list(VIRTUAL_ONLY))
    sidecar._print_cameras()
    rows = [line.split("\t") for line in capsys.readouterr().out.splitlines()]
    assert rows
    assert all(row[2] == "" for row in rows)
