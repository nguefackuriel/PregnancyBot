#!/usr/bin/env python3
"""
export_onnx.py : convertit le CRNN entraîné (PyTorch) en ONNX pour l'inférence
sans PyTorch (onnxruntime, ~15 Mo, CPU ou téléphone).

    python scripts/export_onnx.py --weights data/models/crnn.pt --out data/models/crnn.onnx
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dayone.ocr.crnn import CRNN  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", default=str(ROOT / "data" / "models" / "crnn.pt"))
    ap.add_argument("--out", default=str(ROOT / "data" / "models" / "crnn.onnx"))
    a = ap.parse_args()
    model = CRNN()
    model.load_state_dict(torch.load(a.weights, map_location="cpu")["model"])
    model.eval()
    x = torch.randn(1, 1, 32, 160)
    torch.onnx.export(model, x, a.out, input_names=["image"], output_names=["logits"],
                      dynamic_axes={"image": {0: "batch", 3: "width"}, "logits": {0: "batch", 1: "time"}},
                      opset_version=17, dynamo=False)
    # vérification : mêmes sorties qu'en PyTorch
    import onnxruntime as ort
    sess = ort.InferenceSession(a.out, providers=["CPUExecutionProvider"])
    for w in (96, 200, 400):
        xx = torch.randn(2, 1, 32, w)
        ref = model(xx).detach().numpy()
        out = sess.run(None, {"image": xx.numpy()})[0]
        assert out.shape == ref.shape and np.abs(out - ref).max() < 1e-3, (w, np.abs(out - ref).max())
    print(f"export ok -> {a.out} ({Path(a.out).stat().st_size / 1e6:.1f} Mo)")


if __name__ == "__main__":
    main()
