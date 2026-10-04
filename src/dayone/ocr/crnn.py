"""
CRNN + CTC : un petit modèle de lecture de ligne manuscrite qui tourne sur le téléphone
ou un portable, sans réseau et sans clé. Entraîné de zéro sur des cellules du carnet
(voir scripts/train_crnn.py).

Entrée : image en niveaux de gris, hauteur 32, largeur variable.
Sortie : une chaîne + une confiance (produit des probabilités des caractères émis).
"""
from __future__ import annotations

import math

import cv2
import numpy as np

try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
except Exception:            # absent ou cassé : l'inférence ONNX n'a pas besoin de PyTorch
    torch = None
    nn = None
    F = None

CHARSET = (" !\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~"
           "°éèêëàâäçùûüïîôöœÉÈÀÇÂÊÎÔÛ—–")
BLANK = 0
CHAR2IDX = {c: i + 1 for i, c in enumerate(CHARSET)}
IDX2CHAR = {i + 1: c for i, c in enumerate(CHARSET)}
IMG_H = 32
MAX_W = 640


def encode(text: str) -> list[int]:
    return [CHAR2IDX[c] for c in text if c in CHAR2IDX]


def clean_label(text: str) -> str:
    return "".join(c for c in text if c in CHAR2IDX)


class CRNN(nn.Module if nn is not None else object):
    """VGG réduit -> BiLSTM (2 couches) -> CTC. Environ 2,4 M de paramètres."""

    def __init__(self, n_classes: int = len(CHARSET) + 1, hidden: int = 128):
        super().__init__()
        def block(i, o, pool=None, bn=False):
            layers = [nn.Conv2d(i, o, 3, 1, 1)]
            if bn:
                layers.append(nn.BatchNorm2d(o))
            layers.append(nn.ReLU(inplace=True))
            if pool:
                layers.append(nn.MaxPool2d(pool))
            return layers
        self.cnn = nn.Sequential(
            *block(1, 32, (2, 2)),            # 16 x W/2
            *block(32, 64, (2, 2)),           # 8 x W/4
            *block(64, 128, None, bn=True),
            *block(128, 128, (2, 1)),         # 4 x W/4
            *block(128, 256, None, bn=True),
            *block(256, 256, (2, 1)),         # 2 x W/4
            nn.Conv2d(256, 256, (2, 1)), nn.BatchNorm2d(256), nn.ReLU(inplace=True),   # 1 x W/4
        )
        self.rnn = nn.LSTM(256, hidden, num_layers=2, bidirectional=True, batch_first=True, dropout=0.1)
        self.fc = nn.Linear(hidden * 2, n_classes)

    def forward(self, x):                      # x : B x 1 x 32 x W
        f = self.cnn(x)                        # B x 256 x 1 x W/4
        f = f.squeeze(2).permute(0, 2, 1)      # B x T x 256
        f, _ = self.rnn(f)
        return self.fc(f)                      # B x T x C (logits)


def preprocess(img: np.ndarray, max_w: int = MAX_W) -> np.ndarray:
    """Recadrage de cellule (BGR ou gris) -> float32 1 x 32 x W normalisé [-1, 1]."""
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    h, w = g.shape[:2]
    if h == 0 or w == 0:
        g = np.full((IMG_H, IMG_H), 255, np.uint8)
        h, w = g.shape
    new_w = int(max(16, min(max_w, round(w * IMG_H / h))))
    g = cv2.resize(g, (new_w, IMG_H), interpolation=cv2.INTER_AREA if h > IMG_H else cv2.INTER_CUBIC)
    # normalisation locale : fond -> clair, encre -> sombre, quelle que soit la couleur du papier
    g = g.astype(np.float32)
    lo, hi = np.percentile(g, 2), np.percentile(g, 98)
    if hi - lo < 10:
        hi = lo + 10
    g = np.clip((g - lo) / (hi - lo), 0, 1)
    return (g[None] * 2 - 1).astype(np.float32)


def collate(batch):
    """Liste de (tensor 1x32xW, label str) -> tenseurs rembourrés + cibles CTC."""
    imgs, labels = zip(*batch)
    W = max(i.shape[-1] for i in imgs)
    x = np.ones((len(imgs), 1, IMG_H, W), np.float32)      # 1 = fond clair après normalisation
    for k, im in enumerate(imgs):
        x[k, :, :, : im.shape[-1]] = im
    targets = [torch.tensor(encode(l), dtype=torch.long) for l in labels]
    tlen = torch.tensor([len(t) for t in targets], dtype=torch.long)
    tgt = torch.cat(targets) if targets else torch.zeros(0, dtype=torch.long)
    widths = torch.tensor([i.shape[-1] for i in imgs], dtype=torch.long)
    return torch.from_numpy(x), tgt, tlen, widths


def _log_softmax_np(x: np.ndarray) -> np.ndarray:
    m = x.max(axis=-1, keepdims=True)
    e = np.exp(x - m)
    return x - m - np.log(e.sum(axis=-1, keepdims=True))


def ctc_greedy_decode(logits, width: int | None = None) -> tuple[str, float]:
    """logits T x C (numpy ou torch) -> (texte, confiance).

    Confiance = moyenne géométrique des probabilités des caractères émis.
    """
    arr = logits.detach().cpu().numpy() if hasattr(logits, "detach") else np.asarray(logits)
    probs = _log_softmax_np(arr.astype(np.float32))
    if width is not None:
        T = max(1, int(math.ceil(width / 4)))
        probs = probs[:T]
    best = probs.argmax(-1)
    out, logp, prev = [], [], BLANK
    for t, idx in enumerate(best.tolist()):
        if idx != BLANK and idx != prev:
            out.append(IDX2CHAR.get(idx, ""))
            logp.append(float(probs[t, idx]))
        prev = idx
    text = "".join(out)
    conf = math.exp(sum(logp) / len(logp)) if logp else (float(np.exp(probs[:, BLANK]).mean()) if len(probs) else 0.0)
    return text, round(conf, 3)


class OCR:
    """Inférence : ONNX (onnxruntime, léger) si un .onnx est donné, sinon PyTorch."""

    def __init__(self, weights: str, device: str = "cpu"):
        self.backend = "onnx" if str(weights).endswith(".onnx") else "torch"
        if self.backend == "onnx":
            import onnxruntime as ort
            so = ort.SessionOptions()
            so.intra_op_num_threads = max(1, (__import__("os").cpu_count() or 2) // 1)
            self.sess = ort.InferenceSession(weights, so, providers=["CPUExecutionProvider"])
        else:
            if torch is None:
                raise ImportError("PyTorch absent : utiliser le modèle .onnx")
            self.device = torch.device(device)
            self.model = CRNN().to(self.device)
            state = torch.load(weights, map_location=self.device)
            self.model.load_state_dict(state["model"] if "model" in state else state)
            self.model.eval()

    def _collate_np(self, crops: list[np.ndarray]):
        imgs = [preprocess(c) for c in crops]
        W = max(i.shape[-1] for i in imgs)
        x = np.ones((len(imgs), 1, IMG_H, W), np.float32)
        for k, im in enumerate(imgs):
            x[k, :, :, : im.shape[-1]] = im
        return x, [i.shape[-1] for i in imgs]

    def read(self, crops: list[np.ndarray], batch_size: int = 32) -> list[tuple[str, float]]:
        out = []
        for i in range(0, len(crops), batch_size):
            x, widths = self._collate_np(crops[i: i + batch_size])
            if self.backend == "onnx":
                logits = self.sess.run(None, {"image": x})[0]
            else:
                with torch.no_grad():
                    logits = self.model(torch.from_numpy(x).to(self.device)).cpu().numpy()
            for k in range(len(widths)):
                out.append(ctc_greedy_decode(logits[k], widths[k]))
        return out
