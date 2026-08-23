# Third-party components

`appletd` itself is MIT (see `LICENSE`). Two things it *uses* are not ours, are
not redistributed in this repository, and carry their own terms.

## Depth Anything V2 Small — Core ML conversion

Not in this repository. `./tools/fetch_models.sh` downloads it from Apple's
Hugging Face account at run time, and `models/` is gitignored.

- Conversion: [apple/coreml-depth-anything-v2-small](https://huggingface.co/apple/coreml-depth-anything-v2-small)
- Upstream: [Depth Anything V2](https://github.com/DepthAnything/Depth-Anything-V2)

**Check the upstream licence before shipping a product built on the depth
stream.** Depth Anything V2's smaller checkpoints and its larger ones have not
always carried the same terms, and "it downloaded without asking me anything" is
not a licence grant. The other four streams — hands, body pose, face and person
segmentation — use Apple's Vision framework, which ships in macOS and needs no
model file, so nothing here applies to them.

## pyobjc

The five pinned `pyobjc-framework-*` packages are MIT. They are dependencies
installed by pip, not vendored here.

## Apple Vision and Core ML

Part of macOS, used through their public APIs. Not redistributed. Subject to your
macOS licence agreement.
