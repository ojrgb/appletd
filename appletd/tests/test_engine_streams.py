"""Every stream, on its own, is enough to start an engine.

`HandEngine.__init__` refuses to open the camera with no requests attached, which is
right - a session that runs no inference reads zeros everywhere and says nothing about
why. The refusal was a hand-written chain of `is None` and optical flow was never
added to it, so `Streamflow` alone came back as "no streams enabled" (2026-09-08).

That is the third time a stream has been added without being added to an existing
list, after `appletd.motion` missing from `RUNTIME_MODULES` and the person boxes
missing from `STREAM_CHANNELS`. This is the check that makes the class of mistake
loud: each stream is constructed alone, and each has to be enough.

Stubs, not real detectors - the guard tests for presence, and building a real one
needs a camera, a model file, or both.

Ref: appletd/engine.py.
"""

from __future__ import annotations

import pytest

pytest.importorskip("Vision")

from appletd.engine import EngineError, HandEngine


def _nothing(_frame: object) -> None:
    """A publish callback that discards. The engine only needs it to be callable."""


class _Stub:
    """Stands in for a detector. The guard asks only whether one is there."""


# (the constructor arguments that turn this stream on, its name)
STREAMS: tuple[tuple[dict[str, object], str], ...] = (
    ({"hands": True}, "hands"),
    ({"hands": False, "pose_detector": _Stub(), "on_pose": _nothing}, "pose"),
    ({"hands": False, "face_detector": _Stub(), "on_face": _nothing}, "face"),
    ({"hands": False, "segmentation_detector": _Stub(),
      "on_mask": _nothing}, "segment"),
    ({"hands": False, "flow_detector": _Stub(), "on_flow": _nothing}, "flow"),
    ({"hands": False, "depth_detector": _Stub(), "on_depth": _nothing}, "depth"),
)


@pytest.mark.parametrize(("kwargs", "name"), STREAMS, ids=[n for _k, n in STREAMS])
def test_one_stream_is_enough(kwargs: dict[str, object], name: str) -> None:
    """Constructing must not raise. It opens no camera - `start()` does that."""
    # `type: ignore` on the splat, not on the call: the table above is deliberately
    # one dict of mixed types so each stream reads as one row, and mypy cannot check
    # `**dict[str, object]` against a typed signature. The types it would check are
    # the ones `HandEngine` already declares.
    HandEngine(on_frame=_nothing, **kwargs)   # type: ignore[arg-type]


def test_no_streams_is_still_refused() -> None:
    """The guard has to keep doing its job, or this file proves nothing."""
    with pytest.raises(EngineError, match="no streams enabled"):
        HandEngine(on_frame=_nothing, hands=False)


def test_the_message_names_every_stream() -> None:
    """It used to say "Enable hands, pose, or both" long after there were six."""
    with pytest.raises(EngineError) as caught:
        HandEngine(on_frame=_nothing, hands=False)
    for _kwargs, name in STREAMS:
        assert name in str(caught.value)
