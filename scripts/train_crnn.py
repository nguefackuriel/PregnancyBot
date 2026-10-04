#!/usr/bin/env python3
"""
train_crnn.py : entraîne le lecteur local (CRNN + CTC) sur CPU.

Données : rendu synthétique à la volée (polices du carnet, dégradations photo) +
recadrages réels tirés de la vérité terrain des pages disponibles.
Validation : recadrages réels de patientes mises de côté + un lot synthétique.

    python scripts/train_crnn.py --images "../data/Paper Registry" --steps 4000 --out data/models/crnn.pt
    python scripts/train_crnn.py --resume data/models/crnn.pt --steps 1500   # reprendre

Temps indicatif : ~0,3 s par pas sur 2 cœurs (lot de 48). 4000 pas ≈ 20 min.
"""
import argparse
import random
import sys
import time
from pathlib import Path

import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dayone.ocr.crnn import CRNN, collate, ctc_greedy_decode  # noqa: E402
from dayone.ocr.dataset import RealDataset, SynthDataset, degraded_crops, real_crops  # noqa: E402
from torch.utils.data import ConcatDataset  # noqa: E402
from dayone.ocr.synth import Renderer  # noqa: E402


def cer(a: str, b: str) -> float:
    import difflib
    if not a and not b:
        return 0.0
    sm = difflib.SequenceMatcher(None, a, b)
    return 1 - sm.ratio()


def evaluate(model, loader, device, limit=None):
    model.eval()
    n = ok = 0
    cers = []
    with torch.no_grad():
        for x, tgt, tlen, widths in loader:
            logits = model(x.to(device)).cpu()
            off = 0
            for k in range(x.shape[0]):
                truth = "".join(__import__("dayone.ocr.crnn", fromlist=["IDX2CHAR"]).IDX2CHAR[i] for i in tgt[off: off + int(tlen[k])].tolist())
                off += int(tlen[k])
                pred, _ = ctc_greedy_decode(logits[k], int(widths[k]))
                n += 1
                ok += pred == truth
                cers.append(cer(pred, truth))
            if limit and n >= limit:
                break
    model.train()
    return ok / max(1, n), sum(cers) / max(1, len(cers)), n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", default=str(ROOT.parent / "data" / "Paper Registry"))
    ap.add_argument("--gt", default=str(ROOT / "data" / "ground_truth" / "ground_truth.json"))
    ap.add_argument("--fonts", default=str(ROOT / "data" / "fonts"))
    ap.add_argument("--out", default=str(ROOT / "data" / "models" / "crnn.pt"))
    ap.add_argument("--resume", default=None)
    ap.add_argument("--steps", type=int, default=4000)
    ap.add_argument("--batch", type=int, default=48)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--real-ratio", type=float, default=0.3, help="part de recadrages réels par lot")
    ap.add_argument("--val-patients", default="9,10", help="patientes réservées à la validation")
    ap.add_argument("--synth", type=int, default=30000, help="nombre d'images synthétiques pré-rendues")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--degraded", default="", help="dossier de photos dégradées (scripts/degrade.py) : recadrages après recalage")
    ap.add_argument("--empties", type=int, default=20, help="cellules vides réelles ajoutées par page (étiquette vide)")
    a = ap.parse_args()
    torch.manual_seed(a.seed)
    random.seed(a.seed)
    device = torch.device("cpu")

    # --- données réelles
    items = real_crops(Path(a.gt), Path(a.images), Path(a.fonts), templates_path=ROOT / "data" / "templates.json",
                       empties_per_page=a.empties, seed=a.seed)
    val_p = {int(x) for x in a.val_patients.split(",") if x}
    train_items = [it for it in items if it[2] not in val_p]
    val_items = [it for it in items if it[2] in val_p]
    print(f"recadrages réels : {len(train_items)} entraînement, {len(val_items)} validation (patientes {sorted(val_p)})")
    deg_train, deg_val = [], []
    if a.degraded:
        print("recadrages sur photos dégradées recalées…", flush=True)
        deg = degraded_crops(Path(a.gt), Path(a.degraded), Path(a.fonts), ROOT / "data" / "templates.json", seed=a.seed)
        deg_train = [it for it in deg if it[2] not in val_p]
        deg_val = [it for it in deg if it[2] in val_p]
        print(f"recadrages dégradés : {len(deg_train)} entraînement, {len(deg_val)} validation")
    rng = random.Random(a.seed)
    renderer = Renderer(Path(a.fonts), rng)
    real_ds = RealDataset(train_items, renderer, True)
    if deg_train:
        # déjà dégradés : pas de seconde dégradation
        real_ds = ConcatDataset([real_ds, RealDataset(deg_train, None, False)])
    real_loader = DataLoader(real_ds, batch_size=max(1, int(a.batch * a.real_ratio)),
                             shuffle=True, collate_fn=collate, drop_last=True) if train_items else None
    deg_val_loader = DataLoader(RealDataset([it for it in deg_val if it[1]], None, False), batch_size=64, collate_fn=collate) if deg_val else None
    val_text = [it for it in val_items if it[1]]
    val_empty = [it for it in val_items if not it[1]]
    val_loader = DataLoader(RealDataset(val_text, None, False), batch_size=64, collate_fn=collate) if val_text else None
    # cellules vides de validation, dégradées comme une photo : le modèle doit n'y rien lire
    empty_loader = DataLoader(RealDataset(val_empty, Renderer(Path(a.fonts), random.Random(a.seed + 99)), True),
                              batch_size=64, collate_fn=collate) if val_empty else None
    print("rendu du lot synthétique…", flush=True)
    synth_ds = SynthDataset(Path(a.fonts), Path(a.gt), n=a.synth, seed=a.seed)
    synth_loader = DataLoader(synth_ds, batch_size=a.batch - (real_loader.batch_size if real_loader else 0), shuffle=True,
                              collate_fn=collate, num_workers=0, drop_last=True)
    synth_val = DataLoader(SynthDataset(Path(a.fonts), Path(a.gt), n=320, seed=a.seed + 777, verbose=False), batch_size=64, collate_fn=collate)

    model = CRNN().to(device)
    if a.resume:
        model.load_state_dict(torch.load(a.resume, map_location=device)["model"])
        print("reprise depuis", a.resume)
    n_params = sum(p.numel() for p in model.parameters())
    print(f"paramètres : {n_params / 1e6:.2f} M")
    opt = torch.optim.AdamW(model.parameters(), lr=a.lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=a.lr, total_steps=a.steps, pct_start=0.1)
    ctc = torch.nn.CTCLoss(blank=0, zero_infinity=True)

    def merge(b1, b2):
        x1, t1, l1, w1 = b1
        x2, t2, l2, w2 = b2
        W = max(x1.shape[-1], x2.shape[-1])
        x = torch.ones((x1.shape[0] + x2.shape[0], 1, 32, W), dtype=torch.float32)
        x[: x1.shape[0], :, :, : x1.shape[-1]] = x1
        x[x1.shape[0]:, :, :, : x2.shape[-1]] = x2
        return x, torch.cat([t1, t2]), torch.cat([l1, l2]), torch.cat([w1, w2])

    model.train()
    it_s = iter(synth_loader)
    it_r = iter(real_loader) if real_loader else None
    t0, run = time.time(), 0.0
    best = -1.0
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    for step in range(1, a.steps + 1):
        try:
            batch = next(it_s)
        except StopIteration:
            it_s = iter(synth_loader)
            batch = next(it_s)
        if it_r:
            try:
                rb = next(it_r)
            except StopIteration:
                it_r = iter(real_loader)
                rb = next(it_r)
            batch = merge(batch, rb)
        x, tgt, tlen, widths = batch
        logits = model(x.to(device))                            # B x T x C
        logp = F.log_softmax(logits, dim=-1).permute(1, 0, 2)   # T x B x C
        in_len = torch.clamp((widths + 3) // 4, max=logp.shape[0])
        loss = ctc(logp, tgt, in_len, tlen)
        opt.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
        opt.step()
        sched.step()
        run = 0.98 * run + 0.02 * float(loss.detach()) if step > 1 else float(loss.detach())
        if step % 50 == 0:
            print(f"pas {step:5d}/{a.steps}  perte {run:.3f}  lr {sched.get_last_lr()[0]:.2e}  {time.time() - t0:5.0f}s", flush=True)
        if step % 500 == 0 or step == a.steps:
            sv = evaluate(model, synth_val, device, limit=320)
            msg = f"  synthétique : exact {sv[0]:.3f} CER {sv[1]:.3f} (n={sv[2]})"
            score = sv[0]
            if val_loader:
                rv = evaluate(model, val_loader, device)
                msg += f" | réel (val) : exact {rv[0]:.3f} CER {rv[1]:.3f} (n={rv[2]})"
                score = 0.5 * (sv[0] + rv[0])
            if empty_loader:
                re_ = evaluate(model, empty_loader, device)
                msg += f" | vides lues vides : {re_[0]:.3f} (n={re_[2]})"
            if deg_val_loader:
                rd = evaluate(model, deg_val_loader, device)
                msg += f" | photo dégradée (val) : exact {rd[0]:.3f} CER {rd[1]:.3f} (n={rd[2]})"
            print(msg, flush=True)
            if score >= best:
                best = score
                torch.save({"model": model.state_dict(), "step": step, "score": score}, a.out)
                print(f"  -> sauvegardé {a.out}", flush=True)
    print("terminé, meilleur score", round(best, 3))


if __name__ == "__main__":
    main()
