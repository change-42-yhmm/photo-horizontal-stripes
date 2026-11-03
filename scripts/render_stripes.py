"""Render local horizontal color streaks around a protected photo subject."""

import argparse
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter, ImageOps


def regions(preview, valid, width, height, texture, depth=0):
    """Partition coarse background colors along strong axis-aligned edges."""
    rows, columns = valid.shape
    best = (0.0, None, 0)
    limit, min_rows, min_columns, threshold = {
        "fine": (8, 7, 5, 12),
        "balanced": (7, 8, 7, 15),
        "wide": (5, 11, 10, 22),
    }[texture]
    if depth < limit:
        for axis, size, minimum in ((0, rows, min_rows), (1, columns, min_columns)):
            if size < 2 * minimum:
                continue
            pairs = valid.take(range(1, size), axis) & valid.take(range(size - 1), axis)
            contrast = np.linalg.norm(np.diff(preview, axis=axis), axis=2)
            cross_axis = 1 - axis
            score = (contrast * pairs).sum(axis=cross_axis) / pairs.shape[cross_axis]
            candidates = score[minimum - 1:size - minimum]
            index = int(candidates.argmax())
            if candidates[index] > best[0]:
                best = (candidates[index], axis, index + minimum)

    score, axis, split = best
    if score < threshold:
        yield 0, 0, width, height
        return

    if axis == 0:
        boundary = round(height * split / rows)
        children = (
            (preview[:split], valid[:split], width, boundary, 0, 0),
            (preview[split:], valid[split:], width, height - boundary, 0, boundary),
        )
    else:
        boundary = round(width * split / columns)
        children = (
            (preview[:, :split], valid[:, :split], boundary, height, 0, 0),
            (preview[:, split:], valid[:, split:], width - boundary, height, boundary, 0),
        )
    for colors, support, w, h, x, y in children:
        for left, top, right, bottom in regions(colors, support, w, h, texture, depth + 1):
            yield left + x, top + y, right + x, bottom + y


def row_colors(rgb, eligible, fallback=None):
    counts = eligible.sum(axis=1)
    known = np.flatnonzero(counts)
    if not known.size:
        return fallback
    sums = np.einsum("hwc,hw->hc", rgb, eligible, dtype=np.float64)
    means = sums[known] / counts[known, None]
    return np.column_stack([
        np.interp(np.arange(len(counts)), known, channel) for channel in means.T
    ])


def render(photo, mask, strength, texture):
    rgb = np.asarray(photo)
    alpha = np.asarray(mask)
    if strength == 0 or np.all(alpha == 255):
        return photo.copy()

    width, height = photo.size
    # Exclude translucent edges and their immediate neighbors from color sampling.
    eligible = np.asarray(mask.filter(ImageFilter.MaxFilter(3))) == 0
    if not eligible.any():
        eligible = alpha == 0
    if not eligible.any():
        raise ValueError("The mask leaves no fully unprotected background pixels.")

    preview_size = min(width, 96), min(height, 96)
    coverage = np.asarray(Image.fromarray(eligible.astype(np.float32)).resize(
        preview_size, Image.Resampling.BOX
    ))
    preview = np.stack([
        np.asarray(Image.fromarray((channel * eligible).astype(np.float32)).resize(
            preview_size, Image.Resampling.BOX
        )) / np.maximum(coverage, 0.001)
        for channel in rgb.transpose(2, 0, 1)
    ], axis=2)

    global_rows = row_colors(rgb, eligible)
    output = rgb.copy()
    for left, top, right, bottom in regions(preview, coverage >= 0.5, width, height, texture):
        source = rgb[top:bottom, left:right]
        support = eligible[top:bottom, left:right]
        base = row_colors(source, support, global_rows[top:bottom])
        # Extend a narrower sample through each region to retain small color accents.
        span = right - left
        center = span * 3 // 8
        sample = slice(center, max(center + 1, span * 5 // 8))
        accents = row_colors(source[:, sample], support[:, sample], base)
        y = np.arange(bottom - top)
        if texture == "fine":
            thickness = max(1, round(height / 1300))
            band = np.minimum(y // thickness * thickness + thickness // 2, len(y) - 1)
            stripes = base * 0.45 + accents[band] * 0.55
        else:
            # Give quiet regions thicker bands and keep more detail in varied regions.
            variation = np.linalg.norm(np.std(base, axis=0))
            detail = min(variation / 35, 1)
            thin, thick = (height / 240, height / 85) if texture == "balanced" else (height / 85, height / 32)
            thickness = max(1, round(thick + (thin - thick) * detail))
            if texture == "balanced":
                # Repeatable variation with independent band spacing in each region.
                rng = np.random.default_rng([left, top])
                starts = [0]
                while starts[-1] < len(y):
                    step = max(1, round(thickness * rng.uniform(0.4, 1.65)))
                    starts.append(starts[-1] + step)
                starts = np.asarray(starts[:-1])
            else:
                starts = y[::thickness]
            counts = np.diff(np.append(starts, len(y)))
            colors = base * 0.65 + accents * 0.35
            band_colors = np.add.reduceat(colors, starts, axis=0) / counts[:, None]
            if texture == "balanced":
                # Hold the full sampled color across similar bands until a visible jump.
                held = band_colors[0].copy()
                for color in band_colors[1:]:
                    if np.linalg.norm(color - held) >= 16:
                        held = color.copy()
                    else:
                        color[:] = held
            stripes = np.repeat(band_colors, counts, axis=0)
        weight = (1 - alpha[top:bottom, left:right, None].astype(np.float32) / 255) * strength
        output[top:bottom, left:right] = np.rint(source * (1 - weight) + stripes[:, None] * weight)
    return Image.fromarray(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("photo", type=Path)
    parser.add_argument("mask", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--strength", type=float, default=1)
    parser.add_argument("--texture", choices=("fine", "balanced", "wide"), default="balanced")
    args = parser.parse_args()
    if not 0 <= args.strength <= 1:
        parser.error("--strength must be between 0 and 1.")
    if args.output.suffix.lower() != ".png":
        parser.error("Output must have a .png extension.")

    try:
        with Image.open(args.photo) as source:
            photo = ImageOps.exif_transpose(source).convert("RGB")
            profile = source.info.get("icc_profile") if source.mode == "RGB" else None
        with Image.open(args.mask) as source:
            mask = ImageOps.exif_transpose(source).convert("L")
        if photo.size != mask.size:
            parser.error(f"Mask size {mask.size} must match displayed photo size {photo.size}.")
        result = render(photo, mask, args.strength, args.texture)
        with args.output.open("xb") as destination:
            result.save(destination, format="PNG", icc_profile=profile)
    except (OSError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
