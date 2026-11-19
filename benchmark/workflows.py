"""Native ComfyUI API graphs derived from official T2I templates."""

CLIP_TYPES = {
    "moss": "moss_image",
    "krea2": "krea2",
    "minimax": "minimax",
    "qwen21": "qwen_image",
    "z-image": "lumina2",
    "flux2-9b": "flux2",
}

LATENTS = {
    "moss": "EmptyFlux2LatentImage",
    "krea2": "EmptyLatentImage",
    "qwen21": "EmptyLatentImage",
    "z-image": "EmptySD3LatentImage",
    "flux2-9b": "EmptyFlux2LatentImage",
}

OUTPUT = "11"


def make_workflow(model_id, model, prompt, width, height, seed, prefix):
    graph = {}

    def node(node_id, class_type, **inputs):
        graph[node_id] = {"class_type": class_type, "inputs": inputs}
        return [node_id, 0]

    unet = node("1", "UNETLoader", unet_name=model["unet"], weight_dtype="default")
    clip = node("2", "CLIPLoader", clip_name=model["clip"],
                type=CLIP_TYPES[model_id], device="default")
    vae = node("3", "VAELoader", vae_name=model["vae"])
    size = {"width": width, "height": height}

    if model_id == "minimax":
        positive = node("4", "MiniMaxH3ImageConditioning", clip=clip, vae=vae,
                        prompt=prompt, **size)
        latent = ["4", 1]
    elif model_id == "qwen21":
        positive = node("4", "TextEncodeQwenImage21", clip=clip, prompt=prompt,
                        negative_prompt="", resolution=1024)
        unet = node("8", "QwenImage21Cache", model=unet, device="auto", dtype="default")
    else:
        positive = node("4", "CLIPTextEncode", clip=clip, text=prompt)

    if model_id == "moss":
        negative = node("5", "CLIPTextEncode", clip=clip, text=model["negative"])
    elif model_id == "qwen21":
        negative = ["4", 1]
    else:
        negative = node("5", "ConditioningZeroOut", conditioning=positive)

    if model_id != "minimax":
        latent = node("6", LATENTS[model_id], batch_size=1, **size)
    if model_id == "z-image":
        unet = node("8", "ModelSamplingAuraFlow", model=unet, shift=3.0)

    if model_id == "flux2-9b":
        noise = node("12", "RandomNoise", noise_seed=seed)
        guider = node("13", "CFGGuider", model=unet, positive=positive, negative=negative,
                      cfg=model["cfg"])
        sampler = node("14", "KSamplerSelect", sampler_name=model["sampler"])
        sigmas = node("15", model["scheduler"], steps=model["steps"], **size)
        sampled = node("9", "SamplerCustomAdvanced", noise=noise, guider=guider,
                       sampler=sampler, sigmas=sigmas, latent_image=latent)
    else:
        sampled = node("9", "KSampler", model=unet, positive=positive, negative=negative,
                       latent_image=latent, seed=seed, steps=model["steps"], cfg=model["cfg"],
                       sampler_name=model["sampler"], scheduler=model["scheduler"], denoise=1.0)
    decoded = node("10", "VAEDecode", samples=sampled, vae=vae)
    # SaveImage supports both RGB and RGBA PNGs in the bundled ComfyUI.
    node(OUTPUT, "SaveImage", images=decoded, filename_prefix=prefix)
    return graph


def make_minimax_staged_workflow(model, prompt, jobs):
    """Build one H3 T2I graph that samples all jobs before decoding any of them."""
    width, height, seed, prefix = jobs[0]
    graph = make_workflow("minimax", model, prompt, width, height, seed, prefix)
    sampler = graph.pop("9")
    decoder = graph.pop("10")
    graph.pop(OUTPUT)

    def node(node_id, class_type, **inputs):
        graph[node_id] = {"class_type": class_type, "inputs": inputs}
        return [node_id, 0]

    def after(node_id, signal, value):
        node(node_id, "ImpactExecutionOrderController", signal=signal, value=value)
        return [node_id, 1]

    latents = []
    for index, (width, height, seed, _) in enumerate(jobs, 1):
        if index == 1:
            empty = ["4", 1]
        else:
            # KSampler normalizes this zero latent to H3's 24 channels,
            # one frame and 16x spatial downsampling before generating noise.
            empty = node(f"empty-{index}", "EmptyLatentImage", width=width,
                         height=height, batch_size=1)
            empty = after(f"sample-after-{index}", latents[-1], empty)
        inputs = sampler["inputs"] | {"latent_image": empty, "seed": seed}
        latents.append(node(f"sample-{index}", sampler["class_type"], **inputs))

    outputs = []
    for index, ((_, _, _, prefix), latent) in enumerate(zip(jobs, latents), 1):
        ready = after(f"decode-after-{index}", latents[-1], latent)
        inputs = decoder["inputs"] | {"samples": ready}
        image = node(f"decode-{index}", decoder["class_type"], **inputs)
        output_id = f"save-{index}"
        node(output_id, "SaveImage", images=image, filename_prefix=prefix)
        outputs.append(output_id)
    return graph, outputs
