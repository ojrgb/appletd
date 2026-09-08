"""The multi-person mask arrives as indices and has to be made visible.

`VNInstanceMaskObservation.instanceMask()` is index-encoded — 0 background, 1..4 per
person — so the buffer carries 1, 2, 3, 4 in a uint8 and reads as very nearly black.
Indices are the right thing to send; colouring them is the consumer's job.

The consumer is a generated Script TOP, which no test can run. What IS testable is the
contract between the two: the colour table, and the people count that travels in the
frame's aux block so the consumer knows which kind of mask it is holding.

Ref: appletd/streams.py, tools/td_add_segmentation.py.
"""

from __future__ import annotations

from appletd.streams import MASK_INSTANCE_COLOURS, pack_mask_aux, unpack_mask_people

MAX_PEOPLE = 4


def test_there_is_a_colour_per_person_plus_background() -> None:
    """Vision separates at most four, and index 0 is background."""
    assert len(MASK_INSTANCE_COLOURS) == MAX_PEOPLE + 1
    assert MASK_INSTANCE_COLOURS[0] == (0, 0, 0)


def test_every_colour_is_visible_and_distinct() -> None:
    """A person the same colour as another, or as the background, is invisible."""
    assert len(set(MASK_INSTANCE_COLOURS)) == len(MASK_INSTANCE_COLOURS)
    for index, colour in enumerate(MASK_INSTANCE_COLOURS[1:], start=1):
        assert max(colour) == 255, "person %d does not reach full brightness" % index


def test_every_channel_is_a_byte() -> None:
    for colour in MASK_INSTANCE_COLOURS:
        assert len(colour) == 3
        assert all(0 <= channel <= 255 for channel in colour)


def test_the_people_count_survives_the_aux_block() -> None:
    for people in range(0, MAX_PEOPLE + 1):
        assert unpack_mask_people(pack_mask_aux(people)) == people


def test_an_absent_aux_block_reads_as_a_binary_mask() -> None:
    """The safe answer: 0 means "pass it through", which is what an older writer,
    a depth frame, or a mask written before this existed all want."""
    assert unpack_mask_people(b"") == 0
    assert unpack_mask_people(b"\x01") == 0
    assert unpack_mask_people(b"\x00" * 32) == 0


def test_the_aux_block_fits_what_the_buffer_reserves() -> None:
    from appletd.maskbuf import AUX_BYTES

    assert len(pack_mask_aux(MAX_PEOPLE)) <= AUX_BYTES


def test_the_colouring_maps_each_index_to_its_colour() -> None:
    """The arithmetic the Script TOP does, on a mask with one band per person."""
    import numpy

    height, width = 10, 8
    indices = numpy.zeros((height, width), dtype=numpy.uint8)
    for person in range(1, MAX_PEOPLE + 1):
        indices[person * 2:person * 2 + 2, :] = person

    table = numpy.array(MASK_INSTANCE_COLOURS, dtype=numpy.uint8)
    coloured = table[numpy.clip(indices, 0, len(table) - 1)]

    assert coloured.shape == (height, width, 3)
    for person in range(0, MAX_PEOPLE + 1):
        row = int(numpy.argwhere(indices == person)[0][0])
        assert tuple(coloured[row, 0]) == MASK_INSTANCE_COLOURS[person]


def test_an_index_past_the_table_is_clipped_not_an_exception() -> None:
    """If Vision ever separates a fifth person, a wrong colour beats an IndexError
    inside a cook - which would leave the operator red and the mask gone."""
    import numpy

    table = numpy.array(MASK_INSTANCE_COLOURS, dtype=numpy.uint8)
    rogue = numpy.array([[9]], dtype=numpy.uint8)
    coloured = table[numpy.clip(rogue, 0, len(table) - 1)]
    assert tuple(coloured[0, 0]) == MASK_INSTANCE_COLOURS[-1]
