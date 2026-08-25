# Third-party components

`appletd` itself is MIT (see `LICENSE`). Two things it *uses* are not ours, are
not redistributed in this repository, and carry their own terms.

## Depth Anything V2 Small — Core ML conversion

Not in this repository. `./tools/fetch_models.sh` downloads it from Apple's
Hugging Face account at run time, and `models/` is gitignored.

- Conversion: [apple/coreml-depth-anything-v2-small](https://huggingface.co/apple/coreml-depth-anything-v2-small)
- Upstream: [Depth Anything V2](https://github.com/DepthAnything/Depth-Anything-V2)

**Both are Apache-2.0**, read off the Hugging Face model cards on 2026-08-23:
Apple's Core ML conversion and the upstream `depth-anything/Depth-Anything-V2-Small`.
So the depth stream is usable commercially, with attribution, like this repository.

**The SMALL model only.** Depth Anything V2's Base and Large checkpoints are
CC-BY-NC-4.0 — non-commercial. Swap one in for accuracy and you inherit its terms,
and `tools/fetch_models.sh` fetching something without asking you anything is not a
licence grant.

The other four streams — hands, body pose, face and person segmentation — use Apple's
Vision framework, which ships in macOS and needs no model file, so none of this
applies to them.

## pyobjc

The five pinned `pyobjc-framework-*` packages are MIT. They are dependencies
installed by pip, not vendored here.

## Apple Vision and Core ML

Part of macOS, used through their public APIs. Not redistributed. Subject to your
macOS licence agreement.

## Not affiliated with Apple or Derivative

This is an independent project. It is not affiliated with, endorsed by, sponsored by
or otherwise connected to Apple Inc. or Derivative Inc.

`appletd` is a contraction of "Apple" and "TouchDesigner", chosen because it says what
the thing does. Read cold it could be taken for something Apple publishes, which is why
this section exists rather than being left to be obvious. Apple, Vision, Core ML,
macOS and the Neural Engine are trademarks of Apple Inc.; TouchDesigner and Derivative
are trademarks of Derivative Inc. They are used here to say which software this works
with, and for no other reason. All marks belong to their owners.
