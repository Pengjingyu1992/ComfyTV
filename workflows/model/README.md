**English** | [简体中文](README.zh.md)

# `model/` workflows

Workflows in this folder appear in the **3D Model Stage** dropdown. Take one upstream image (plus optional text / model inputs); produce a 3D asset. The stage's preview viewport renders whatever the workflow saves — meshes (`.glb`, `.gltf`, `.obj`, …), Gaussian splats (`.spz`, `.splat`, `.ksplat`, splat-`.ply`) and point clouds (`.ply`) are all supported, so mesh, splat and point-map workflows share this one folder.

## Stage inputs

- **Prompt** — optional; none of the shipped image-to-3D workflows consume it, but prompt-driven text-to-3D workflows can bind it.
- **Upstream images** — the shipped workflows lift `images.image0` into 3D; **Pixal3D Multi-View** also reads images 2–4 as the left, back and right views.
- **Upstream text / models** (optional) — for workflows that take references.
- **Custom params** — quality knobs each preset exposes (see below); anything not exposed stays at the official template's default.

## What your workflow needs

- A `SaveGLB` (or compatible save node emitting a `ui["3d"]` result) at the end — splat pipelines get there via `SplatToFile3D`.
- A `LoadImage` for the upstream image.

To add your own workflow see [docs/custom-workflows.md](../../docs/custom-workflows.md); to configure per-node bindings, select the stage on the canvas and open the left **ComfyTV** sidebar — see [docs/sidebar-config-editor.md](../../docs/sidebar-config-editor.md).

## What's here today

All of them are adapted from ComfyUI's official bundled templates, flattened (subgraphs/preview branches removed) with the official parameter defaults kept. Common custom param across generative workflows: `seed` (randomized per Run when unset).

- **Hunyuan3D 2.1** (`hunyuan3d-21.json` + `_preset.json`) — image → untextured **mesh** GLB. Official `3d_hunyuan3d-v2.1` template defaults: 30 steps, cfg 5, euler/normal, latent resolution 4096, decoder num_chunks 8000 / octree_resolution 256, surface-net threshold 0.6. Custom params: `seed`, `steps`, `guidance`. Model: `checkpoints/hunyuan_3d_v2.1.safetensors`.
- **TripoSplat Gaussian** (`triposplat-gaussian.json` + `_preset.json`) — image → **3D Gaussian Splat** (`.spz`). Background is always auto-removed via BiRefNet before preprocessing (erode 1, size 1024 — the model's training resolution). Sampler defaults: 20 steps, cfg 3, dpmpp_2m/simple; decode produces 262144 gaussians (octree density — higher oversamples, no new detail). Custom params: `seed`, `steps`, `guidance`, `num_gaussians`. Models: `diffusion_models/triposplat_fp16.safetensors`, `clip_vision/dino_v3_vit_h.safetensors`, `vae/triposplat_vae_decoder_fp16.safetensors`, `vae/flux2-vae.safetensors`, `background_removal/birefnet.safetensors`.
- **MoGe-2 Depth Mesh** (`moge2-mesh.json` + `_preset.json`) — image → textured **point-map mesh** GLB of the visible surfaces (scene relief from monocular geometry estimation, not a closed object). Deterministic, no seed. Defaults: resolution_level 9, fov auto-recovered, decimation 1, discontinuity threshold 0.04, texture on. Custom param: `resolution_level` (0 = fastest … 9 = most detail). Model: `geometry_estimation/moge_2_vitl_normal_fp16.safetensors`.
- **TRELLIS.2** (`trellis2.json` + `_preset.json`) — image → vertex-colored **mesh** GLB. From the official `3d_pixal3d_trellis2_image_to_model` template with its TRELLIS.2 switch on; background auto-removed via BiRefNet, `Trellis2Conditioning`. Model: `diffusion_models/trellis_2_int8_convrot.safetensors`. Tested working (~97 s on a 5090).
- **Pixal3D** (`pixal3d.json` + `_preset.json`) — same template with its Pixal3D switch on: MoGe-2 estimates the photo's horizontal field of view and `Pixal3DConditioning` uses it. Models: `diffusion_models/pixal3d_int8_convrot.safetensors`, `geometry_estimation/moge_2_vitl_normal_fp16.safetensors`. Tested working (~82 s).
- **Pixal3D Multi-View** (`pixal3d-multiview.json` + `_preset.json`) — images 1–4 are the **front, left, back and right** views of one object; each is cut out and framed like the single-image path, then `Pixal3DMultiViewConditioning` (fov 20, the official default for multi-view generators and rig renders). Unused view slots are pruned, so 1–4 views all run; the first connected view sets the front. From the official `3d_pixal3d_multi_views` template. Model: `diffusion_models/pixal3d_multiview_int8_convrot.safetensors`. Tested working (2 views, ~95 s).

All three share the TRELLIS.2 pipeline (shape VAE + texture VAE + DINOv3 conditioning), then remesh 768 → decimate to 200k faces → vertex paint → GLB; only the main model and its conditioning differ. Custom param: `seed`.

Shared files for these three: `vae/trellis_2_shape_vae_bf16.safetensors`, `vae/trellis_2_texture_vae_bf16.safetensors`, `clip_vision/dino_v3_L_naf_fp32.safetensors`, `background_removal/birefnet.safetensors` — <https://huggingface.co/Comfy-Org/Pixal3D> and <https://huggingface.co/Comfy-Org/TRELLIS.2>.

## Models referenced

Download URLs are embedded in each workflow's node `properties.models`; see also [docs/models.md](../../docs/models.md).
