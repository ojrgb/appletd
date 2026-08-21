"""Synthetic faces: a `FaceFrame` from a handful of numbers. No camera, no Vision.

The face counterpart of `synth.py` and `synth_body.py`, and much smaller than
either, because the face contract is currently the OBSERVATION's own numbers -
bounding box, head pose, confidence, quality - and none of that needs geometry. A
box and three angles.

WHAT IT DOES NOT DO: landmark points. They are not published yet (the per-region
point counts need one camera frame to settle - see `face_types.py`), so
synthesising them would be inventing the very numbers that module refuses to guess.
When the counts land, this is where a plausible 76-point face goes, and
`tools/send_synthetic_face.py` will pick it up with no changes.

Thread: pure functions over immutable values. Safe anywhere.
Ref: DESIGN.md 6.4, visionhands/face_types.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from visionhands.face_types import MAX_FACES, Face, FaceFrame, order_faces
from visionhands.types import Confidence, NormX, NormY

# A head's width as a fraction of the frame at `size` = 1.0, and its height. Taller
# than wide, as a head is - a square box would make any aspect-sensitive consumer
# look correct here and wrong on a real face.
_WIDTH: Final = 0.22
_HEIGHT: Final = 0.30


@dataclass(frozen=True)
class FacePose:
    """The parameters of one synthetic face.

    center_x, center_y  where the head's CENTRE sits, 0..1, origin bottom-left
    size                scale on the bounding box; 1.0 is a head at conversational
                        distance from a 1280x720 camera
    roll, yaw, pitch    DEGREES, as the channels carry them - Vision's radians are
                        converted in face.py, and a synthetic stream that emitted
                        radians would be the one place that conversion was not
                        exercised end to end
    quality             faceCaptureQuality, so a test can drive a face below a gate
    """

    center_x: float = 0.5
    center_y: float = 0.55
    size: float = 1.0
    roll: float = 0.0
    yaw: float = 0.0
    pitch: float = 0.0
    quality: float = 0.8


def synthetic_face(pose: FacePose) -> Face:
    """One `FacePose` -> one immutable `Face`. Pure.

    Contract: the bounding box is normalised with the origin BOTTOM LEFT and
              `bbox_y` is its BOTTOM edge, exactly as `VNFaceObservation` reports it
              (DESIGN.md 7). Clamped into frame, because Vision's box is always
              inside the image and a consumer tuned against 1.4 would be tuned
              against something that cannot happen.
    """
    width = _WIDTH * pose.size
    height = _HEIGHT * pose.size
    left = min(1.0 - width, max(0.0, pose.center_x - width * 0.5))
    bottom = min(1.0 - height, max(0.0, pose.center_y - height * 0.5))
    return Face(
        # 1.0: what a detected face is expected to report. UNMEASURED for faces
        # (DESIGN.md 2.12), which is why `quality` is the one to gate on and the
        # one this dataclass lets a caller vary.
        confidence=Confidence(1.0),
        quality=Confidence(pose.quality),
        roll_deg=pose.roll,
        yaw_deg=pose.yaw,
        pitch_deg=pose.pitch,
        bbox_x=NormX(left),
        bbox_y=NormY(bottom),
        bbox_w=width,
        bbox_h=height,
        landmarks=(),
        found=True,
    )


def synthetic_face_frame(poses: tuple[FacePose, ...], seq: int = 1,
                         captured_at: float = 0.0, width: int = 1280,
                         height: int = 720) -> FaceFrame:
    """A whole frame from up to MAX_FACES poses, ordered as the engine orders them.

    Runs the real `order_faces`, so this stream has the same left-to-right slot rule
    the live one has - the same reason `tools/send_synthetic.py` runs the real
    `SlotAssigner` rather than publishing its own order.
    """
    return FaceFrame(
        seq=seq, captured_at=captured_at, width=width, height=height,
        faces=order_faces([synthetic_face(pose) for pose in poses[:MAX_FACES]]),
    )
