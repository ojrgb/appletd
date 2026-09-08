"""Which points a line is drawn between, for the overlays.

One table per skeleton, and everything the overlay needs is derived from it: the
Select CHOP's channel list, the CHOP to SOP's channel scope, and the Delete SOP's
primitive list. They cannot drift apart because there is only one of them.

HOW THE DRAWING WORKS, because the Delete list is otherwise unreadable. A skeleton
is a list of SEGMENTS, and each segment is written out as its two endpoints, so 21
segments become 42 points. `CHOP to SOP` in `lines` mode joins consecutive points -
point 0 to 1, 1 to 2, 2 to 3 - which gives one primitive per gap and TWICE as many
as wanted. Half of them are wrong.

The odd-numbered gaps are the wrong ones: they join the END of one segment to the
START of the next. Most are harmless, because the skeleton shares points - the thumb
ends where the next segment begins, so the gap is zero length and draws nothing. The
few that jump - a fingertip back to the wrist, or the last point of one hand to the
first of the next - are real lines across the image, and `jump_primitives()` is
exactly those.

So the Delete SOP removes 11 primitives for two hands, not 83, and the zero-length
ones are left where they are: cheaper to leave than to name.

Pure stdlib: no pyobjc, no TouchDesigner, so a test can check the whole thing.

Thread: pure functions over immutable values. Safe anywhere.
Ref: tools/td_add_overlay.py, docs/ATTRIBUTES.md.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Final

from appletd.face_types import FACE_KEYPOINTS, FACE_REGIONS
from appletd.pose_types import BODY_JOINT_NAMES
from appletd.types import JOINT_NAMES

# The hand, as MediaPipe draws it. Our joint table is in MediaPipe's order, so these
# are its `HAND_CONNECTIONS` with our names substituted - the same 21 lines anybody
# comparing the two would expect to see.
#
# Written as NAMES rather than the indices MediaPipe uses: an index is silently wrong
# if the joint table ever changes, and `_self_check()` below holds every name in here
# against the table it came from.
HAND_CONNECTIONS: Final[tuple[tuple[str, str], ...]] = (
    ("wrist", "thumb_cmc"), ("thumb_cmc", "thumb_mp"),
    ("thumb_mp", "thumb_ip"), ("thumb_ip", "thumb_tip"),
    ("wrist", "index_mcp"), ("index_mcp", "index_pip"),
    ("index_pip", "index_dip"), ("index_dip", "index_tip"),
    ("index_mcp", "middle_mcp"), ("middle_mcp", "middle_pip"),
    ("middle_pip", "middle_dip"), ("middle_dip", "middle_tip"),
    ("middle_mcp", "ring_mcp"), ("ring_mcp", "ring_pip"),
    ("ring_pip", "ring_dip"), ("ring_dip", "ring_tip"),
    ("ring_mcp", "little_mcp"), ("little_mcp", "little_pip"),
    ("little_pip", "little_dip"), ("little_dip", "little_tip"),
    # The palm, closing the loop back to the wrist.
    ("wrist", "little_mcp"),
)

# One finger, for the `Index Line` mode. The index chain from the wrist out, which is
# what "the index line" means when the rest of the hand is not drawn.
INDEX_CONNECTIONS: Final[tuple[tuple[str, str], ...]] = (
    ("wrist", "index_mcp"), ("index_mcp", "index_pip"),
    ("index_pip", "index_dip"), ("index_dip", "index_tip"),
)

# The body. Vision publishes no bones, so this is a reading of the 19 joints: head to
# neck, neck to the shoulders and down the spine, and a limb chain from each.
BODY_CONNECTIONS: Final[tuple[tuple[str, str], ...]] = (
    ("nose", "left_eye"), ("left_eye", "left_ear"),
    ("nose", "right_eye"), ("right_eye", "right_ear"),
    ("nose", "neck"),
    ("neck", "left_shoulder"), ("neck", "right_shoulder"), ("neck", "root"),
    ("root", "left_hip"), ("root", "right_hip"),
    ("left_shoulder", "left_elbow"), ("left_elbow", "left_wrist"),
    ("right_shoulder", "right_elbow"), ("right_elbow", "right_wrist"),
    ("left_hip", "left_knee"), ("left_knee", "left_ankle"),
    ("right_hip", "right_knee"), ("right_knee", "right_ankle"),
)

# THE FACE AS THE COMPONENT USUALLY SHIPS IT: four key points, not 76 landmarks.
#
# `Facekeypoints` is ON by default and strips the 348 landmark channels at the stream,
# so an overlay drawn from landmarks selects NOTHING in a default project - which is
# what it did. These four are what actually reaches the overlay.
#
# A Y rather than a triangle: the eye line across, both eyes down to the tip of the
# nose, and the nose down to the mouth. Four segments read as a face at a glance,
# which is all a marker overlay has to do. There is no midpoint between the eyes to
# draw from - a Select CHOP does no arithmetic - so both eyes go to the nose instead.
FACE_KEYPOINT_CONNECTIONS: Final[tuple[tuple[str, str], ...]] = (
    ("eye_left", "eye_right"),
    ("eye_left", "nose_tip"),
    ("eye_right", "nose_tip"),
    ("nose_tip", "mouth"),
)

# The face regions that are a LOOP rather than an arc, so the last point joins the
# first. The eyes and both lip rings close; the contour, the crest and the median
# line are arcs and must not, or a line is drawn across the face.
CLOSED_FACE_REGIONS: Final[frozenset[str]] = frozenset({
    "left_eye", "right_eye", "outer_lips", "inner_lips",
})


def face_landmark_connections() -> tuple[tuple[str, str], ...]:
    """All 76 landmarks, derived from the region table rather than written out.

    Only reachable with `Facekeypoints` OFF - it is on by default and strips these
    channels at the stream. `FACE_KEYPOINT_CONNECTIONS` is what a default project can
    draw.

    Each region is a chain through its own points, in the order Vision publishes
    them, closed for the four that are loops. A one-point region - the pupils - has
    no segments and contributes nothing.

    Point names carry the two-digit index the channels use, so `left_eye_00` here is
    `f0_left_eye_00_tx` on the wire.
    """
    pairs: list[tuple[str, str]] = []
    for region in FACE_REGIONS:
        # `point_count` stays None until somebody MEASURES it (face_types.py), and a
        # region with no count publishes no channels - so there is nothing to join.
        if region.point_count is None:
            continue
        points = ["%s_%02d" % (region.name, i) for i in range(region.point_count)]
        if len(points) < 2:
            continue
        pairs += list(pairwise(points))
        if region.name in CLOSED_FACE_REGIONS:
            pairs.append((points[-1], points[0]))
    return tuple(pairs)


def endpoints(prefixes: tuple[str, ...],
              connections: tuple[tuple[str, str], ...]) -> list[str]:
    """Every segment's two endpoints, in draw order: `h0_wrist`, `h0_thumb_cmc`, ...

    One flat list across every prefix, because the drawing is one polyline through
    all of them - the seam between `h0` and `h1` is just another jump.
    """
    return ["%s_%s" % (prefix, point)
            for prefix in prefixes
            for pair in connections for point in pair]


def channel_names(prefixes: tuple[str, ...],
                  connections: tuple[tuple[str, str], ...],
                  space: str = "t") -> list[str]:
    """The Select CHOP's list: every X, then every Y.

    Both halves are the same points in the same order, so a Shuffle splitting the
    list in half puts each point's pair on one sample.

    `space` is `t` for world coordinates, `p` for pixels, or `""` for the raw
    normalised pair. World is the default: it is what a render wants, and it is
    already scaled by `Orthowidth`.
    """
    points = endpoints(prefixes, connections)
    return ([name + "_%sx" % space for name in points]
            + [name + "_%sy" % space for name in points])


def jump_primitives(prefixes: tuple[str, ...],
                    connections: tuple[tuple[str, str], ...]) -> list[int]:
    """The primitives `CHOP to SOP` draws that the skeleton did not ask for.

    The odd-numbered gaps, minus the ones that are zero length because the skeleton
    shares that point. See the module docstring.
    """
    points = endpoints(prefixes, connections)
    return [index for index in range(1, len(points) - 1, 2)
            if points[index] != points[index + 1]]


def _self_check() -> None:
    """Every joint named here exists in the table it came from.

    These tables are a second description of the contracts in `types.py` and
    `pose_types.py`, and the failure is silent: a renamed joint would produce a
    Select CHOP naming a channel nobody publishes, which emits NOTHING for that
    term - a skeleton quietly missing a bone, with no error anywhere.
    """
    for label, names, table in (
            ("HAND_CONNECTIONS", JOINT_NAMES, HAND_CONNECTIONS),
            ("INDEX_CONNECTIONS", JOINT_NAMES, INDEX_CONNECTIONS),
            ("BODY_CONNECTIONS", BODY_JOINT_NAMES, BODY_CONNECTIONS),
            ("FACE_KEYPOINT_CONNECTIONS", FACE_KEYPOINTS,
             FACE_KEYPOINT_CONNECTIONS)):
        unknown = sorted({point for pair in table for point in pair}
                         - set(names))
        if unknown:
            raise RuntimeError("%s names joints that do not exist: %s"
                               % (label, " ".join(unknown)))


_self_check()
