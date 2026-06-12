# Kronos Model Notes

Working notes on the Kronos paper (Shi et al., *Kronos: A Foundation Model for the Language of Financial Markets*, arXiv:2508.02739, Tsinghua) — what the model actually does mathematically, what it was trained on, and what that implies for this platform's use of it.

## The two-stage design

Kronos treats candlesticks as a *language*: a tokenizer turns each bar into discrete tokens, then a decoder-only Transformer (GPT-style) learns to predict the next token. Forecasting = text generation.

### Stage 1 — K-line tokenizer (Binary Spherical Quantization)

Each timestep's OHLCV(+amount) vector is encoded by a Transformer autoencoder into a continuous latent `ξt`, then quantized by **BSQ** (a lookup-free quantization variant): the latent is projected onto `k = 20` learnable hyperplanes and each projection's *sign* becomes one bit, giving a binary code `bt ∈ {−1,+1}²⁰` — an implicit vocabulary of 2²⁰ ≈ 1.05M "words" with no codebook-collapse problem.

A 1M-entry softmax is too expensive for the autoregressive head, so the code is **factorized into two subtokens**: coarse `bᶜ` (10 bits) and fine `bᶠ` (10 bits), each with vocab 2¹⁰ = 1024. The hierarchy is *imposed by the training loss*, not hoped for:

```
L_tokenizer = L_coarse + L_fine + λ·L_quant
  L_coarse = E‖x − Dec(bᶜ)‖²        coarse subtoken alone must reconstruct a
                                     low-fidelity version (trend, level)
  L_fine   = E‖x − Dec([bᶜ,bᶠ])‖²   full token must reconstruct high fidelity
                                     → fine subtoken learns the residual
  L_quant  = BSQ commitment (L2 between ξ and its binary code)
```

So every bar becomes two integers: "roughly what happened" + "the detail."

### Stage 2 — hierarchical autoregressive Transformer

A causal decoder-only Transformer models the token sequence:

```
p(b) = ∏ₜ p(bt | b<t)                          (standard LM factorization)
p(bt | b<t) = p(bᶜt | b<t) · p(bᶠt | b<t, bᶜt)  (coarse-to-fine chain rule)
```

Per step, the two subtoken embeddings are fused (`vi = W·[eᶜ; eᶠ]`) as input. The fine head conditions on the **sampled** coarse token via cross-attention (query = sampled coarse embedding, key/value = history) — sampling rather than teacher-forcing during training reduces exposure bias for multi-step rollouts. Training objective is plain NLL over both subtokens.

Model family (context length ≤ **512 tokens**, 1 token = 1 bar):

| | Layers | d_model | Params |
|---|---|---|---|
| Kronos-small (we run this) | 8 | 512 | 24.7M |
| Kronos-base (fine-tune target) | 12 | 832 | 102.3M |
| Kronos-large | 18 | 1664 | 499.2M |

### Inference

LLM-style generation: temperature + nucleus (top-p) sampling, then **Monte Carlo averaging** — sample N future trajectories, decode each back to continuous OHLCV through the tokenizer decoder, average for a stable forecast. The paper's settings for price/return forecasting: **T = 0.6, top-p = 0.90, N = 10**.

### Why discrete tokens beat regression (the key ablation)

| Variant | Objective | Price IC |
|---|---|---|
| Direct-AR (continuous) | MSE | 0.0212 |
| Prob-AR (continuous) | NLL | 0.0179 |
| Kronos-Parallel (subtokens predicted together) | CE | 0.0345 |
| **Kronos (sequential coarse→fine)** | CE | **0.0431** |

MSE regression collapses to the conditional mean — on noisy, multimodal financial data that's a near-constant forecast. Cross-entropy over a discrete vocabulary models the *full distribution* of next-bar outcomes, and sampling from it preserves the multimodality. The coarse→fine ordering alone is worth +25%.

## Training corpus — NSE is in it

12.11B bars, 45+ exchanges, 7 granularities, with a cleaning pipeline (filters abnormal spikes, inactivity) and rebalancing that down-weights crypto/futures/forex.

| Relevant slice | Assets | Observations | Granularities | From |
|---|---|---|---|---|
| **NSE (XNSE)** | 2,554 stocks/ETFs | 242.4M | **5T**, 15T, 30T, H, D, W | 2020-01-31 |
| BSE | 5,491 | 284.4M | 5T…W | 2020-01-31 |
| India indices | 113 | 3.2M | 5T…W | 2020-01-31 |
| NYSE/Nasdaq | ~15.8K | 4.6B | **T(1-min)**…W | 2000 |
| Shanghai/Shenzhen | ~6.6K | 3.7B | **T(1-min)**…W | 1990 |

XNSE also appears in the per-exchange zero-shot evaluation tables with IC ≈ 0.03–0.06 — positive and best-in-class, but small in absolute terms (normal for financial forecasting).

## Implications for this platform

1. **"Zero-shot on NSE" was a mischaracterization.** Kronos has seen ~242M NSE bars (plus BSE and Indian indices). The market itself is *in-distribution*. What it has **never seen is NSE at 1-minute granularity** — its 1-min experience comes from US/China/crypto only — and **no NSE data after the corpus cutoff**.
2. **We feed it out-of-distribution input.** `score_from_db()` sends 400 × 1-minute NSE bars. The frequency-matched alternative: aggregate to 5-minute bars (the paper's own protocol uses lookback 480 at 5T), forecast ~6 × 5-min ≈ the same 30-minute gating horizon. Bonus: 480 five-minute bars ≈ 5+ trading days of context vs ~1 day now.
3. **Our sampling parameters differ from the paper's task-tuned ones.** We run T=1.0, N=5; the paper uses **T=0.6, N=10** for directional forecasting. Lower temperature sharpens the directional estimate. Worth an A/B in shadow mode (both changes are config/one-liners in `kronos_signal.py`).
4. **The fine-tune case is narrower but still real:** granularity adaptation (1-min NSE), recency (post-corpus data), and liquid-universe focus — not "teaching it NSE" wholesale. The three-way backtest (ORB vs +zero-shot vs +fine-tuned) remains the deciding instrument.
5. **Calibrated expectations:** zero-shot IC of 0.03–0.06 is a *small* edge. It can still be valuable as a gate (filtering, not predicting), which is precisely what the shadow-mode calibration loop is designed to measure.
