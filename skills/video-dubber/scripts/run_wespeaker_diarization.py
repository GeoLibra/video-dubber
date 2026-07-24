#!/usr/bin/env python3
"""Run WeSpeaker diarization in its isolated, compatible environment."""

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", default="english")
    parser.add_argument("--speaker-count", type=int, default=0)
    parser.add_argument("--device", default="auto")
    args = parser.parse_args()

    import wespeaker
    import wespeaker.cli.speaker as speaker_module

    if args.speaker_count:
        from wespeaker.diar.spectral_clusterer import cluster as spectral_cluster

        # The stock CLI uses automatic UMAP/HDBSCAN clustering. For a known
        # interview cast, official WeSpeaker spectral clustering with a fixed
        # count is more stable and avoids splitting one voice into extra roles.
        speaker_module.cluster = lambda embeddings: spectral_cluster(
            embeddings,
            num_spks=args.speaker_count,
            min_num_spks=args.speaker_count,
            max_num_spks=args.speaker_count,
        )

    model = wespeaker.load_model(args.model)
    if args.device == "auto":
        import torch
        device = "mps" if torch.backends.mps.is_available() else "cpu"
    else:
        device = args.device
    model.set_device(device)
    result = model.diarize(args.audio, "audio")
    turns = [
        {
            "start": round(float(start), 3),
            "end": round(float(end), 3),
            "label": int(label),
        }
        for _utt, start, end, label in result
        if float(end) > float(start)
    ]
    Path(args.output).write_text(
        json.dumps(
            {
                "model": args.model,
                "device": device,
                "speaker_count": args.speaker_count,
                "turns": turns,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
