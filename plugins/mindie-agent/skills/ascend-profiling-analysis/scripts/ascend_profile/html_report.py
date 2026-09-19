#!/usr/bin/env python3
"""Shared report data, timing calculations and operator evidence.

The current HTML renderer lives in html_report_v2. This module owns the
source-backed data helpers it uses; it has no legacy rendering entrypoint.
"""

from __future__ import annotations

# Observe the real CLI before optional runtime imports; copied remote helpers stay standalone.

import bisect
import csv
import html
import json
import os
import re
import statistics
import sys

from pathlib import Path
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

try:
    from ascend_profile import metrics, models, rules, store  # type: ignore
except ImportError:  # pragma: no cover - allow running from scripts/ directly
    import metrics  # type: ignore[no-redef]
    import models  # type: ignore[no-redef]
    import rules  # type: ignore[no-redef]
    import store  # type: ignore[no-redef]

csv.field_size_limit(10 * 1024 * 1024)

PALETTE = {
    "bg": "#0d1117",
    "bg_card": "#161b22",
    "bg_card_alt": "#1c232c",
    "border": "#30363d",
    "text": "#e6edf3",
    "muted": "#8b949e",
    "accent": "#58a6ff",
    "warn": "#f0883e",
    "danger": "#f85149",
    "success": "#3fb950",
}

FAMILY_COLOR = {
    "attention_moe_workload": "#58a6ff",
    "moe_or_dummy_workload": "#d2a8ff",
    "attention_dense_workload": "#3fb950",
    "ffn_or_dummy_workload": "#f0883e",
    "communication_only": "#f85149",
    "mixed_workload": "#bc8cff",
}

BLOCK_COLOR = {
    "attention": "#58a6ff",
    "moe": "#d2a8ff",
    "ffn": "#f0883e",
    "aicpu": "#8b949e",
    "other": "#484f58",
}

OP_TYPE_COLOR = {
    "aic": "#79c0ff",
    "aiv": "#d2a8ff",
    "mix_cv": "#ffa657",
    "mix_comm_aiv": "#f0883e",
    "communication": "#f85149",
    "aicpu": "#a371f7",
    "dsa": "#7ee787",
    "unknown": "#8b949e",
}

BOUND_FAMILY_COLOR = {
    "cube": "#79c0ff",
    "vector": "#d2a8ff",
    "aic_mte": "#58a6ff",
    "aiv_mte": "#bc8cff",
    "scalar": "#ffa657",
    "mixed": "#f0883e",
    "communication": "#f85149",
    "comm_aiv_mix": "#ff7b72",
    "aicpu": "#a371f7",
    "dsa": "#7ee787",
    "unknown": "#8b949e",
}

# kernel_details 字段文档：hover tooltip 显示
FIELD_DOC = {
    "self_layer_pct": "本算子**单次**执行耗时 / 当前 layer 在本 rank 上所有 device 事件 (AIV/AIC/mix_cv/mix_comm_aiv/communication/aicpu) 的 active union 时长（去 redundant，跨流取并集）。问的是：'这一次调用占当前 layer 多少？'",
    "klayer_pct": "本类 kernel（按 short_op_name 聚合：aclnn 算子保留到第一个 `_` 前的 API 名，如 `aclnnCausalConv1d_*` → `aclnnCausalConv1d`；HCCL 算子保留到第一个 `__` 前，如 `hcom_allReduce__503_150_1` → `hcom_allReduce`；其余保持原名）在**当前 layer 内**所有调用的 union 耗时 / 当前 layer 同口径的 active union 时长。括号里是该类 kernel 在本 layer 内的调用次数。",
    "kstep_pct": "本类 kernel 在**当前 step 内（本 rank 上）**所有调用的 union 耗时 / 当前 step 在本 rank 上所有 device 事件 (AIV/AIC/mix_cv/mix_comm_aiv/communication/aicpu) 的 active union 时长（去 redundant，跨流取并集，不含 bubble）。括号里是该类 kernel 在本 step 内的总调用次数。问的是：'这类算子在整个 step 中（不含 bubble）占多少？' 这是评估 kernel 在一次 forward pass 中重要性的核心指标。",
    "ep_peak_to_mean": "EP 峰均比 = max(rank GMM 总耗时) / mean(rank GMM 总耗时)，>= 1。GroupedMatmul 是 MoE 各 expert dispatch 后的核心计算 kernel，每 rank 上的 GMM 总耗时直接反映该 rank 被分到的 token 量。经验阈值：>1.10 视为 EP 不均；>1.30 严重热点。",
    "ep_per_rank_gmm": "该 rank 上所有 GroupedMatmul/GroupedMatmulV5 算子的 wall-time 总和。",
    "speculative_layer": "投机解码 (speculative decoding) 的辅助层。layer_role = 'speculative' / 'spec' / 'spec_layer' 时被归入此类。投机层用 draft model 提前并行预测 N 个 token，主模型用一次大 forward 验证哪些预测正确。",
    "duration_us": "算子在 device 上的实际执行时间 (μs)。来自 kernel_details.csv 的 Duration 列。",
    "wait_us": "算子从就绪到真正开始执行之间的等待时间 (μs)。常见来源：等 stream sync、等 HCCL Notify Wait、等数据依赖；wait 过大通常说明 host bound 或上游 collective 慢。",
    "stream_id": "Device 侧执行流 ID。同一 rank 上不同 stream 可以并行执行；'N/A' 通常代表 HCCL 默认通信流。",
    "aicore_time": "AIC（AI Core，Cube）流水线累计时间。",
    "aiv_time": "AIV（AI Vector）流水线累计时间。",
    "aic_mac_time": "AIC MAC pipe（矩阵乘单元）累计耗时。计算 bound 的关键 stage；mac 高说明真正的 GEMM compute 在跑。",
    "aic_fixpipe_time": "AIC FixPipe（结果搬出 + 后处理 + cast）累计耗时。",
    "aic_mte1_time": "AIC MTE1：L1 → BT/SMEM 等核内搬运。",
    "aic_mte2_time": "AIC MTE2：外部存储 → L1（DDR/L2 → L1）。GroupedMatmul / MatMul 等大算子常 mte2 bound，说明 DDR 带宽是瓶颈。",
    "aic_scalar_time": "AIC scalar pipe：取指、地址计算、控制流。scalar 偏高通常说明 kernel 中 control flow 太重 或 block_dim 切分不合理。",
    "aiv_vec_time": "AIV Vector pipe：逐元素向量运算（add/mul/exp 等）。",
    "aiv_mte2_time": "AIV MTE2：外部存储 → UB（Unified Buffer）。",
    "aiv_mte3_time": "AIV MTE3：UB → 外部存储。",
    "aiv_scalar_time": "AIV scalar pipe（同 AIC scalar，作用在 AIV 上）。",
    "shape_signature": "算子输入/输出 tensor shape 的归一化签名，shape-strict 分组用。",
    "op_type": "算子大类：aic=纯 AIC（Cube 核）；aiv=纯 AIV（Vector 核）；mix_cv=AIC+AIV 混合（FlashAttention、GroupedMatmul 等）；mix_comm_aiv=通信+AIV 融合（dispatch/combine）；communication=纯 HCCL；aicpu=芯片上的标量 AI CPU 核（device 侧，不是 host CPU）。",
    "bound_stage": "该算子流水线中累计耗时最长的单 stage，通常就是该 kernel 的性能瓶颈。",
    "bound_family": "bound_stage 的粗粒度归类：cube / vector / aic_mte / aiv_mte / scalar / mixed / communication / ...",
    "comm_share": "该范围内 HCCL 算子（+ mix_comm_aiv）累计耗时 / 该范围 wall_ms。衡量通信开销占比。",
    "block_kind": "Layer 内部 block 的功能分类：attention / ffn / moe（aicpu / other 会被合并到相邻 block）。",
    "companion_layer": "该 layer 缺少 attention block，通常是陪跑 / dummy 数据 / warmup 等结构。",
}

# AIC / AIV stage 分组：用于在算子卡里按归属展示
AIC_STAGES = ["aic_mac_time", "aic_fixpipe_time", "aic_mte1_time", "aic_mte2_time", "aic_scalar_time"]
AIV_STAGES = ["aiv_vec_time", "aiv_mte2_time", "aiv_mte3_time", "aiv_scalar_time"]

# bound_stage → 决策依据字段映射
STAGE_FAMILY = {
    "aic_mac_time": "cube",
    "aic_fixpipe_time": "aic_mte",
    "aic_mte1_time": "aic_mte",
    "aic_mte2_time": "aic_mte",
    "aic_scalar_time": "scalar",
    "aiv_vec_time": "vector",
    "aiv_mte2_time": "aiv_mte",
    "aiv_mte3_time": "aiv_mte",
    "aiv_scalar_time": "scalar",
}

# pipeline stage → 对应 ratio 列名（原始 kernel_details.csv 字段名）
STAGE_RATIO_FIELD = {
    "aic_mac_time": "aic_mac_ratio",
    "aic_scalar_time": "aic_scalar_ratio",
    "aic_mte1_time": "aic_mte1_ratio",
    "aic_mte2_time": "aic_mte2_ratio",
    "aic_fixpipe_time": "aic_fixpipe_ratio",
    "aiv_vec_time": "aiv_vec_ratio",
    "aiv_scalar_time": "aiv_scalar_ratio",
    "aiv_mte2_time": "aiv_mte2_ratio",
    "aiv_mte3_time": "aiv_mte3_ratio",
}

# 原始 kernel_details.csv 完整 schema (46 列)，用于算子卡 tier 3 的 raw dump
RAW_KD_FIELDS = [
    "Device_id", "Model ID", "Task ID", "Stream ID", "Name", "Type", "OP State",
    "Accelerator Core", "Start Time(us)", "Duration(us)", "Wait Time(us)",
    "Block Dim", "Mix Block Dim", "HF32 Eligible",
    "Input Shapes", "Input Data Types", "Input Formats",
    "Output Shapes", "Output Data Types", "Output Formats",
    "Context ID",
    "aicore_time(us)", "aic_total_cycles",
    "aic_mac_time(us)", "aic_mac_ratio",
    "aic_scalar_time(us)", "aic_scalar_ratio",
    "aic_mte1_time(us)", "aic_mte1_ratio",
    "aic_mte2_time(us)", "aic_mte2_ratio",
    "aic_fixpipe_time(us)", "aic_fixpipe_ratio",
    "aic_icache_miss_rate",
    "aiv_time(us)", "aiv_total_cycles",
    "aiv_vec_time(us)", "aiv_vec_ratio",
    "aiv_scalar_time(us)", "aiv_scalar_ratio",
    "aiv_mte2_time(us)", "aiv_mte2_ratio",
    "aiv_mte3_time(us)", "aiv_mte3_ratio",
    "aiv_icache_miss_rate",
    "cube_utilization(%)",
]

# Ratio / utilization 字段说明（用于 hover tooltip）
RATIO_FIELD_DOC = {
    "aic_mac_ratio": "AIC MAC pipe 利用率 = aic_mac_time / aicore_time。接近 1 说明该 kernel 真在做 cube 计算；偏低则 cube 没吃满。",
    "aic_scalar_ratio": "AIC scalar pipe 利用率。偏高（>0.5）通常意味着 kernel 中控制流 / 索引计算过重 → 优化方向：合并 scalar、对齐 block_dim。",
    "aic_mte1_ratio": "AIC MTE1 利用率。L1↔BT 内部搬运占比。",
    "aic_mte2_ratio": "AIC MTE2 利用率。外存→L1 搬运占比；偏高（>0.6）说明 DDR/L2 带宽瓶颈。",
    "aic_fixpipe_ratio": "AIC FixPipe 利用率。结果搬出 + 后处理占比；矩阵乘类算子偏高常见。",
    "aiv_vec_ratio": "AIV Vector pipe 利用率。算子真正做向量计算的占比。",
    "aiv_scalar_ratio": "AIV scalar 利用率。同 AIC scalar，作用在 AIV 上。",
    "aiv_mte2_ratio": "AIV MTE2 利用率。外存→UB 搬运占比；偏高说明算子读外存压力大。",
    "aiv_mte3_ratio": "AIV MTE3 利用率。UB→外存 搬运占比；偏高说明算子写外存压力大。",
    "aic_icache_miss_rate": "AIC instruction cache miss 率。偏高说明 kernel 指令体过大 → block_dim 切得太细 / 重复编译。",
    "aiv_icache_miss_rate": "AIV instruction cache miss 率。同上。",
    "cube_utilization(%)": "Cube 单元的整体占比（百分制）。GEMM 类算子衡量真实计算密度。",
    "Block Dim": "AIC block dimension（拆 N 维度的并发数）。",
    "Mix Block Dim": "AIV block dimension（mix 算子时使用）。",
    "HF32 Eligible": "该算子能否使用 HF32 精度。",
    "Context ID": "执行上下文 ID（runtime 调度用）。",
    "Input Shapes": "输入 tensor 形状列表。",
    "Input Data Types": "输入 tensor dtype 列表。",
    "Input Formats": "输入 tensor format（ND / NCHW / FRACTAL_NZ ...）。",
    "Output Shapes": "输出 tensor 形状。",
    "Output Data Types": "输出 tensor dtype。",
    "Output Formats": "输出 tensor format。",
}
FIELD_DOC.update(RATIO_FIELD_DOC)


def short_op_name(name: str) -> str:
    """Normalize kernel name for grouping. Strategy: keep the original name.

    Only strip auto-generated suffixes that prevent aggregation:
      - aclgraph naming (`aclnn<API>_<OpsImpl>_<KernelName>`) → keep `aclnn<API>`
        e.g. `aclnnCausalConv1d_CausalConv1d_CausalConv1d` → `aclnnCausalConv1d`
              `aclnnInplaceFillScalar_FillAiCore_Fill` → `aclnnInplaceFillScalar`
      - HCCL sequence id (`hcom_<op>__<seq>_<group>_<idx>`) → keep `hcom_<op>`
        e.g. `hcom_allReduce__503_150_1` → `hcom_allReduce`
    Everything else (Triton-style kernels, bare op names) → original.
    """
    if not name:
        return ""
    if name.startswith("aclnn"):
        return name.split("_", 1)[0]
    if name.startswith("hcom_"):
        return name.split("__", 1)[0] if "__" in name else name
    return name


def short_rank_label(rank_id: str) -> str:
    parts = {}
    for tok in rank_id.split("_"):
        m = re.match(r"([a-z]+)(\d+)$", tok)
        if m:
            parts[m.group(1)] = int(m.group(2))
    bits = []
    if "dp" in parts:
        bits.append(f"dp{parts['dp']}")
    if "tp" in parts:
        bits.append(f"tp{parts['tp']}")
    if "pp" in parts and parts["pp"] != 0:
        bits.append(f"pp{parts['pp']}")
    if "ep" in parts:
        bits.append(f"ep{parts['ep']}")
    return "·".join(bits) if bits else rank_id


def family_label(family: str, layer_count: int | None = None) -> str:
    base = {
        "attention_moe_workload": "Attention + MoE",
        "moe_or_dummy_workload": "MoE-only / Dummy",
        "attention_dense_workload": "Attention + Dense",
        "ffn_or_dummy_workload": "FFN-only / Dummy",
        "communication_only": "Communication-only",
        "mixed_workload": "Mixed",
    }.get(family, family.replace("_", " ").title())
    if layer_count:
        return f"{base} · {layer_count}L"
    return base


def fmt_ms(v, prec=2):
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{prec}f}"
    except Exception:
        return str(v)


# -----------------------------
# v7 analysis helpers
#
# NOTE (UI-only heuristics):
#   The functions in this block (``compute_ep_balance``,
#   ``assess_companion_run``, ``detect_attention_subtype``,
#   ``derive_layer_composition``, ``guess_model_structure``) compute
#   heuristic signals for the HTML report's narrative cards. They are
#   *not* formal diagnosis findings: nothing here is added to
#   ``diagnosis_findings.json`` and they do not participate in the
#   evidence-chain validator. Treat their outputs as UI hints; load
#   ``diagnosis_findings.json`` if you need official claims with
#   ``evidence_ids`` / ``alignment_ids`` / ``limitations`` attached.
#
#   These hints are rendered alongside an explicit "UI-only heuristic"
#   ribbon in the HTML so end-users don't mistake them for findings.
# -----------------------------

def compute_ep_balance(b) -> dict:
    """Compute EP load balance via GroupedMatmul wall-time per rank.

    Returns {by_rank, mean_us, peak_us, min_us, peak_to_mean, spread}.
    peak_to_mean (>=1) is the standard "EP imbalance" indicator — values close to 1
    mean ranks are balanced; >= 1.10 means at least one rank does noticeably more
    GMM work than the average (an EP hotspot rank).
    """
    by_rank: dict[str, float] = defaultdict(float)
    for e in b.events:
        if getattr(e, "redundant", False):
            continue
        nm = (e.name or "")
        if "GroupedMatmul" in nm:
            by_rank[e.rank_id] += e.duration_us
    if not by_rank:
        return {"by_rank": {}, "mean_us": 0.0, "peak_us": 0.0, "min_us": 0.0,
                "peak_to_mean": 1.0, "spread": 0.0, "available": False}
    vals = list(by_rank.values())
    mean = sum(vals) / len(vals)
    peak = max(vals)
    lo = min(vals)
    return {
        "by_rank": dict(by_rank),
        "mean_us": mean,
        "peak_us": peak,
        "min_us": lo,
        "peak_to_mean": (peak / mean) if mean > 0 else 1.0,
        "spread": ((peak - lo) / mean) if mean > 0 else 0.0,
        "available": True,
    }


def assess_companion_run(b) -> dict:
    """Identify step-indices where some ranks run real data while others ran dummy.

    Returns {companion_step_indices, n_companion, n_total_aligned, rank_family_counts,
             companion_rank_pairs}.
    """
    step_by_rank: dict[str, list] = defaultdict(list)
    for s in b.step_summary:
        step_by_rank[s["rank_id"]].append(s)
    for rid in step_by_rank:
        step_by_rank[rid].sort(key=lambda x: safe_float(x["start_us"]))
    if not step_by_rank:
        return {"companion_step_indices": [], "n_companion": 0, "n_total_aligned": 0,
                "rank_family_counts": {}, "companion_rank_pairs": []}
    rank_ids = sorted(step_by_rank.keys())
    n_steps = min(len(step_by_rank[r]) for r in rank_ids)
    real_set = {"attention_moe_workload", "attention_dense_workload"}
    dummy_set = {"moe_or_dummy_workload", "ffn_or_dummy_workload"}
    companion_indices = []
    pair_counts: Counter = Counter()
    for i in range(n_steps):
        real_ranks = []
        dummy_ranks = []
        for r in rank_ids:
            fam = step_by_rank[r][i].get("step_family", "")
            if fam in real_set:
                real_ranks.append(r)
            elif fam in dummy_set:
                dummy_ranks.append(r)
        if real_ranks and dummy_ranks:
            companion_indices.append(i)
            pair_counts[(tuple(real_ranks), tuple(dummy_ranks))] += 1
    rank_family_counts: dict[str, dict] = {}
    for r in rank_ids:
        rank_family_counts[r] = Counter(
            s.get("step_family", "") for s in step_by_rank[r]
        )
    return {
        "companion_step_indices": companion_indices,
        "n_companion": len(companion_indices),
        "n_total_aligned": n_steps,
        "rank_family_counts": {r: dict(c) for r, c in rank_family_counts.items()},
        "companion_rank_pairs": [
            {"real_ranks": list(rr), "dummy_ranks": list(dd), "count": int(c)}
            for (rr, dd), c in pair_counts.most_common(8)
        ],
    }


def attention_categories_for_events(events: Iterable[Event]) -> set[str]:
    """Union of ``op_categories`` for the given events, as input to
    ``resolve_attention_family``.

    Real pipeline events already carry the categories assigned at normalize
    time (``NormalizedEvent.op_categories``), so we union those directly
    instead of re-running ``rules.categories_and_roles`` per event.
    Duck-typed stand-ins that lack ``op_categories`` (unit-test fakes with
    only ``name`` / ``task_type`` / ``accel_core``) still fall back to the
    legacy per-event classification so the test contract is unchanged.
    """
    cats: set[str] = set()
    for event in events:
        op_categories = getattr(event, "op_categories", None)
        if op_categories is not None:
            cats.update(op_categories)
            continue
        cat_tuple, _ = rules.categories_and_roles(
            short_op_name(event.name),
            getattr(event, "task_type", "") or "",
            getattr(event, "accel_core", "") or "",
        )
        cats.update(cat_tuple)
    return cats


def detect_attention_subtype(b, row_start: int, row_end: int, rank_id: str) -> str:
    """Decide the paper-aligned attention family for a block by reusing
    the canonical category-driven resolver in
    ``rules.resolve_attention_family``.

    Returns one of: ``csa`` / ``hca`` / ``dsa`` / ``mla`` / ``linear`` /
    ``gqa_or_mha`` / ``attn``. A trailing ``+kvc`` suffix indicates the
    Hamming-distance KV-compression overlay is active.

    The CANN / vllm-ascend implementation routes both DSA and CSA / HCA
    through ``AscendSFABackend`` (``sfa_v1.py``); we keep the paper
    names in the report and document the backend identity in
    ``attention_families.yaml``.

    Earlier drafts of this function did raw kernel-name substring
    matching, which diverged from the YAML / unit-test contract on two
    edge cases:
      * ``UnpadFlashAttention`` returned ``fa`` here but is mapped to
        ``attention.flash_score`` by ``kernel_signatures.yaml`` (because
        it's the long-context branch of the dense
        ``AscendAttentionBackend``, NOT a separate FA backend);
      * blocks containing only ``KVQuantSparseAttnSharedKVMetadata``
        satisfied ``has_sparse_sharedkv`` because of the loose
        ``sharedkv`` substring — yet the YAML contract requires the
        main category (``attention.sparse_sharedkv``), not the metadata
        sub-category.
    Routing the decision through ``categories_and_roles`` eliminates
    both divergences.

    Decision order is loaded from
    ``knowledge/attention_families.yaml:cheat_sheet.resolver``.

    Shape-based refinement of ``gqa_or_mha``:
        After the category resolver returns its terminal label, if the
        family is ``gqa_or_mha`` we make one best-effort attempt to
        upgrade it to ``mha`` / ``gqa`` / ``mqa`` by reading the Q/K
        Input Shapes recorded in ``kernel_details.csv`` for the FIA /
        UnpadFA events in this block (see
        ``rules.refine_dense_attention_from_shapes``). When shapes are
        missing or fail sanity checks we keep the umbrella
        ``gqa_or_mha``. The refinement is a heuristic — it depends on
        CANN serialising Input Shapes correctly and on us latching onto
        the right Q/K tensors — so we never override the
        category-based decision for anything other than ``gqa_or_mha``.
    """
    events = events_in_row_range(b.events, row_start, row_end, rank_id)
    cats = attention_categories_for_events(events)
    family = rules.resolve_attention_family(cats)
    # Best-effort shape refinement; only acts on the dense umbrella
    # label (and its `+kvc` variant) so it never disturbs MLA / CSA /
    # HCA / DSA / linear decisions.
    if family == "gqa_or_mha" or family.startswith("gqa_or_mha+"):
        refined = rules.refine_dense_attention_from_shapes(events)
        if refined != "gqa_or_mha":
            family = family.replace("gqa_or_mha", refined, 1)
    return family


def derive_layer_composition(b, ls: dict) -> str:
    """Derive layer composition from block_segments, e.g. 'gqa_or_mha+moe', 'mla+ffn', 'moe'.

    Falls back to '—' when no blocks are recorded under this layer.
    """
    rid = ls["rank_id"]
    r_start = int(safe_float(ls["row_start"]))
    r_end = int(safe_float(ls["row_end"]))
    blocks = block_segments_in_layer(b, rid, r_start, r_end)
    blocks.sort(key=lambda x: int(safe_float(x["row_start"])))
    parts = []
    for bs in blocks:
        kind = (bs.get("block_kind") or "").lower()
        if kind == "attention":
            sub = detect_attention_subtype(
                b,
                int(safe_float(bs["row_start"])),
                int(safe_float(bs["row_end"])),
                rid,
            )
            parts.append(sub)
        elif kind == "moe":
            parts.append("moe")
        elif kind in ("ffn", "mlp", "dense"):
            parts.append("ffn")
        elif kind:
            parts.append(kind)
    return "+".join(parts) if parts else "—"


def guess_model_structure(b, step_row: dict) -> str | None:
    """Honest structural fingerprint — *not* a model name guess.

    Returns 'NL · <attn_sub>+<ffn_or_moe>' if attention subtype is detectable, else None.
    The naming has been deliberately downgraded from "model id" → "structure" because
    structurally-different checkpoints (e.g. DeepSeek-V2-Lite 27L MLA vs Qwen-3.5 MoE 27L FIA)
    used to collide on (layer_count, has_attn, has_moe).
    """
    layer_count = int(safe_float(step_row.get("main_layer_count")))
    has_attn = str(step_row.get("has_attention", "")).lower() == "true"
    has_moe = str(step_row.get("has_moe", "")).lower() == "true"
    if not has_attn and not has_moe:
        return None
    rid = step_row["rank_id"]
    # use the step's row range as the inspection window
    seg_id = step_row.get("segment_id")
    seg = b._step_seg_by_id.get(seg_id) if seg_id else None
    if seg is None:
        return f"{layer_count}L · ?+{'moe' if has_moe else ('ffn' if has_attn else '?')}"
    attn_sub = detect_attention_subtype(
        b,
        int(safe_float(seg["row_start"])),
        int(safe_float(seg["row_end"])),
        rid,
    ) if has_attn else None
    rhs = "moe" if has_moe else ("ffn" if has_attn else "?")
    if has_attn:
        return f"{layer_count}L · {attn_sub}+{rhs}"
    return f"{layer_count}L · {rhs}"


# kept as alias for older callers
guess_model_id = guess_model_structure


# Everything in normalized_event_index.csv (i.e. originally from kernel_details.csv +
# HCCL traces) runs on device — AIV/AIC/mix_cv/mix_comm_aiv/communication/aicpu.
# `aicpu` is the chip's scalar AI CPU core (not host CPU) and must be counted as
# device-active. Only host-side events (Python / dispatcher / launch overhead,
# which are NOT in our event index) would be excluded — we never get them here.


def union_duration_us(events) -> float:
    """Merge-intervals union of event time spans (microseconds).

    Use this instead of `sum(e.duration_us)` whenever you want a real
    "active wall" for a section — summing double-counts events that happen
    concurrently on different streams (e.g. AIC stream + AIV stream both
    busy at the same wall-clock μs).

    Counts all non-redundant events. redundant flag is set by `dedup_comm_aiv`
    to mark dual-stream copies of an HCCL event so they're not double-counted.
    """
    intervals = []
    for e in events:
        if getattr(e, "redundant", False):
            continue
        if e.end_us <= e.start_us:
            continue
        intervals.append((e.start_us, e.end_us))
    if not intervals:
        return 0.0
    intervals.sort()
    total = 0.0
    cur_s, cur_e = intervals[0]
    for s, end in intervals[1:]:
        if s <= cur_e:
            if end > cur_e:
                cur_e = end
        else:
            total += cur_e - cur_s
            cur_s, cur_e = s, end
    total += cur_e - cur_s
    return total


def union_duration_us_by_name(events) -> dict:
    """Group events by short_op_name, return union duration per group (device-wide)."""
    by_name = defaultdict(list)
    for e in events:
        if getattr(e, "redundant", False):
            continue
        by_name[short_op_name(e.name)].append(e)
    return {k: union_duration_us(v) for k, v in by_name.items()}


def split_main_speculative_tail(b, step_seg: dict, rank_id: str) -> dict:
    """For a given step segment, split events into: head / main / speculative / tail / bubble buckets.

    Returns durations + event lists per bucket (events already rank-filtered).
    Speculative = events inside layers tagged as speculative.
    """
    step_row_start = step_seg["row_start"]
    step_row_end = step_seg["row_end"]
    step_events = events_in_row_range(b.events, step_row_start, step_row_end, rank_id)

    seg_id = step_seg["segment_id"]
    anatomy = b._step_anatomy_by_id.get(seg_id)
    head_row_start = int(safe_float((anatomy or {}).get("head_row_start") or step_row_start))
    head_row_end   = int(safe_float((anatomy or {}).get("head_row_end") or step_row_start))
    main_row_start = int(safe_float((anatomy or {}).get("main_row_start") or step_row_start))
    main_row_end   = int(safe_float((anatomy or {}).get("main_row_end") or step_row_end))
    tail_row_start = int(safe_float((anatomy or {}).get("tail_row_start") or step_row_end))
    tail_row_end   = int(safe_float((anatomy or {}).get("tail_row_end") or step_row_end))

    # speculative layers within this step
    spec_layers = [
        ls for ls in layer_segments_in_step(b, rank_id, step_row_start, step_row_end)
        if ls.get("layer_role") in ("speculative", "spec", "spec_layer")
    ]
    spec_rows = set()
    for ls in spec_layers:
        for rr in range(int(ls["row_start"]), int(ls["row_end"])):
            spec_rows.add(rr)

    def in_range(e, rs, re_):
        return rs <= e.row_idx < re_

    head_evts, main_evts, spec_evts, tail_evts = [], [], [], []
    for e in step_events:
        if e.row_idx in spec_rows:
            spec_evts.append(e)
        elif in_range(e, head_row_start, head_row_end):
            head_evts.append(e)
        elif in_range(e, tail_row_start, tail_row_end):
            tail_evts.append(e)
        elif in_range(e, main_row_start, main_row_end):
            main_evts.append(e)

    head_us = union_duration_us(head_evts)
    main_us = union_duration_us(main_evts)
    spec_us = union_duration_us(spec_evts)
    tail_us = union_duration_us(tail_evts)
    step_busy_us = union_duration_us(step_events)

    return {
        "step_events": step_events,
        "head_events": head_evts,
        "main_events": main_evts,
        "spec_events": spec_evts,
        "tail_events": tail_evts,
        "spec_layer_count": len(spec_layers),
        "head_us":  head_us,
        "main_us":  main_us,
        "spec_us":  spec_us,
        "tail_us":  tail_us,
        "step_busy_us": step_busy_us,
        "head_bubble_ms": safe_float((anatomy or {}).get("head_bubble_ms", 0)),
        "main_bubble_ms": safe_float((anatomy or {}).get("main_bubble_ms", 0)),
        "tail_bubble_ms": safe_float((anatomy or {}).get("tail_bubble_ms", 0)),
        "step_wall_ms": safe_float(step_seg.get("wall_ms", 0)) or (safe_float(step_seg["end_us"]) - safe_float(step_seg["start_us"])) / 1000.0,
    }


def kernel_rollup_by_bound(events: list) -> list:
    """Roll up events by (op_type, kernel name family) with bound-stage majority.

    Returns sorted list (desc by duration_us) of:
        {kernel, op_type, count, duration_us, bound_family, dominant_stage}
    """
    by_key: dict = defaultdict(lambda: {
        "count": 0,
        "duration_us": 0.0,
        "wait_us": 0.0,
        "op_type": "",
        "stage_durations": defaultdict(float),
    })
    for e in events:
        if getattr(e, "redundant", False):
            continue
        key = (short_op_name(e.name), e.op_type)
        rec = by_key[key]
        rec["count"] += 1
        rec["duration_us"] += e.duration_us
        rec["wait_us"] += getattr(e, "wait_us", 0)
        rec["op_type"] = e.op_type
        for stage_field, v in (e.pipeline or {}).items():
            rec["stage_durations"][stage_field] += safe_float(v)
    rows = []
    for (kernel, op_type), rec in by_key.items():
        bound = pick_bound_stage(rec["stage_durations"]) if rec["stage_durations"] else None
        family = STAGE_FAMILY.get(bound, "unknown") if bound else "unknown"
        rows.append({
            "kernel": kernel,
            "op_type": op_type,
            "count": rec["count"],
            "duration_us": rec["duration_us"],
            "wait_us": rec["wait_us"],
            "bound_stage": bound or "—",
            "bound_family": family,
        })
    rows.sort(key=lambda r: -r["duration_us"])
    return rows


def safe_float(v, default=0.0):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def hue_shift(base_hex: str, shift: int) -> str:
    base_hex = base_hex.lstrip("#")
    r = int(base_hex[0:2], 16)
    g = int(base_hex[2:4], 16)
    b = int(base_hex[4:6], 16)
    if shift >= 0:
        f = shift / 100.0
        r = int(r + (255 - r) * f)
        g = int(g + (255 - g) * f)
        b = int(b + (255 - b) * f)
    else:
        f = 1.0 + shift / 100.0
        r = int(r * f)
        g = int(g * f)
        b = int(b * f)
    return f"#{r:02x}{g:02x}{b:02x}"


def class_color(family, step_class_id):
    base = FAMILY_COLOR.get(family, "#58a6ff")
    if not step_class_id:
        return base
    bucket = sum(ord(c) for c in step_class_id) % 5
    offset = [-18, -9, 0, 9, 18][bucket]
    return hue_shift(base, offset)


def load_csv(path: Path):
    # Data layer lives in ``store`` now; ``utf-8-sig`` there is a strict
    # superset of the plain ``utf-8`` we used here.
    return store.csv_rows(path)


def load_json(path: Path):
    return store.read_json(path)


@dataclass
class Event:
    """Render-side view of one normalized event.

    Loaded via ``metrics.load_events_csv`` (the shared data layer) and
    adapted through ``from_normalized``. The adapter exists because the
    renderers use short field names (``name`` / ``accel_core`` /
    ``pipeline``) and need two mutable render-only fields (``redundant``
    set by ``dedup_comm_aiv``, ``raw_row`` attached from the source
    kernel_details.csv); ``NormalizedEvent`` is frozen and pipeline-wide,
    so subclassing it is not an option. ``op_roles`` / ``op_categories``
    arrive as parsed tuples (not raw CSV strings) — nothing in this module
    reads them, they are kept for debugging symmetry with
    ``NormalizedEvent``.
    """

    event_id: str
    rank_id: str
    source_id: str
    row_idx: int
    name: str
    task_type: str
    op_type: str
    accel_core: str
    stream_id: str
    start_us: float
    end_us: float
    duration_us: float
    wait_us: float
    pipeline: dict
    shape_signature: str
    op_roles: tuple = ()
    op_categories: tuple = ()
    redundant: bool = False  # 通信去重 flag
    raw_row: dict = field(default_factory=dict)  # full kernel_details.csv row (46 fields)

    @classmethod
    def from_normalized(cls, event: "models.NormalizedEvent") -> "Event":
        return cls(
            event_id=event.event_id,
            rank_id=event.rank_id,
            source_id=event.source_id,
            row_idx=event.row_idx,
            name=event.name_raw,
            task_type=event.task_type,
            op_type=event.op_type,
            accel_core=event.accelerator_core,
            stream_id=event.stream_id,
            start_us=event.start_us,
            end_us=event.end_us,
            duration_us=event.duration_us,
            wait_us=event.wait_us,
            pipeline=dict(event.pipeline_us),
            shape_signature=event.shape_signature or "",
            op_roles=event.op_roles,
            op_categories=event.op_categories,
        )


@dataclass
class Bundle:
    root: Path
    rank_summary: list = field(default_factory=list)
    step_summary: list = field(default_factory=list)
    step_anatomy: list = field(default_factory=list)
    step_class: list = field(default_factory=list)
    layer_class: list = field(default_factory=list)
    block_class: list = field(default_factory=list)
    operator_class: list = field(default_factory=list)
    hccl_class: list = field(default_factory=list)
    hccl_op: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    manifest: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    step_segments: list = field(default_factory=list)
    layer_segments: list = field(default_factory=list)
    block_segments: list = field(default_factory=list)
    # Per-rank, row_start-sorted layer_segments / block_segments / step_segments +
    # parallel row_start arrays for bisect-based "events fully inside [rs, re]"
    # membership lookups, plus segment_id and step_segment_id maps. Without
    # these the L2 / L3 renderers scan the full lists once per step:
    #
    # * layer_segments scan in render_l2 / split_main_speculative_tail
    # * block_segments scan in derive_layer_composition (called per layer)
    # * step_segments / step_anatomy linear next() lookups
    #
    # On the prefill_analyse case (21 k step segments × 65 k layer segments) this
    # was ~1.4 B comparisons and triggered the 30-min report-stage timeout.
    _layer_segs_by_rank: dict = field(default_factory=dict)
    _layer_seg_row_starts_by_rank: dict = field(default_factory=dict)
    _layer_seg_row_ends_by_rank: dict = field(default_factory=dict)
    _block_segs_by_rank: dict = field(default_factory=dict)
    _block_seg_row_starts_by_rank: dict = field(default_factory=dict)
    _block_seg_row_ends_by_rank: dict = field(default_factory=dict)
    _step_seg_by_id: dict = field(default_factory=dict)
    _step_anatomy_by_id: dict = field(default_factory=dict)


def _load_segments(path: Path, key: str) -> list:
    # Segment payloads stay as plain dicts here: every renderer consumes
    # them via ``.get(...)``/``[...]``, so materializing the ``metrics``
    # dataclasses would force attribute-style rewrites across the whole
    # module for zero behavioural gain.
    data = load_json(path)
    if data is None:
        return []
    if isinstance(data, dict) and key in data:
        return data[key]
    if isinstance(data, list):
        return data
    return []


def _load_events(path: Path) -> list:
    """Load normalized events through the shared data layer
    (``metrics.load_events_csv``) and adapt them to the render-side
    ``Event`` view."""

    if not path.exists():
        return []
    return [Event.from_normalized(event) for event in metrics.load_events_csv(path)]


def _l3_rep_seg_ids(b: "Bundle") -> list[str]:
    """Representative step segment ids that get L3 views.

    Top-3 step classes by ``wall_ms_sum``; the representative member is the
    one whose ``wall_ms`` is closest to the class mean. Shared by
    ``render_l3_views`` (rendering) and ``_raw_rows_needed`` (lazy raw-row
    loading) so the two never drift apart.
    """
    if not b.step_class:
        return []
    rep_seg_ids: list[str] = []
    classes_sorted = sorted(b.step_class, key=lambda r: safe_float(r["wall_ms_sum"]), reverse=True)
    L3_TOP_N = 3
    for cls in classes_sorted[:L3_TOP_N]:
        cls_id = cls["step_class_id"]
        members = [s for s in b.step_summary if s.get("step_class_id") == cls_id]
        if not members:
            continue
        target = safe_float(cls["wall_ms_mean"])
        rep = min(members, key=lambda x: abs(safe_float(x["wall_ms"]) - target))
        rep_seg_ids.append(rep["segment_id"])
    return rep_seg_ids


def _raw_rows_needed(b: "Bundle") -> dict[str, set[int]]:
    """Compute the ``{source_id: {row_idx, ...}}`` raw-row set the renderers
    actually read.

    ``raw_row`` has exactly two consumers:
      1. L3 operator cards — rendered only for events inside the layers of
         each step-class representative step (see ``render_l3_views`` /
         ``_render_l3_layer``);
      2. ``rules.refine_dense_attention_from_shapes`` — reads only events
         whose name carries a flash-score token (FIA / UnpadFA / ...), via
         ``detect_attention_subtype``.

    Every other event's ``raw_row`` is never read, so the loader can skip it
    instead of materialising the full raw kernel_details source per rank.
    """
    needed: dict[str, set[int]] = defaultdict(set)
    step_rank_by_seg = {s.get("segment_id"): s.get("rank_id") for s in b.step_summary}
    for seg_id in _l3_rep_seg_ids(b):
        step_meta = b._step_seg_by_id.get(seg_id)
        rank_id = step_rank_by_seg.get(seg_id)
        if not step_meta or rank_id is None:
            continue
        for ls in layer_segments_in_step(b, rank_id, int(step_meta["row_start"]), int(step_meta["row_end"])):
            for e in events_in_row_range(b.events, ls.get("row_start", 0), ls.get("row_end", 0), rank_id):
                needed[e.source_id].add(e.row_idx)
    # Same selection as ``rules.refine_dense_attention_from_shapes``:
    # lowercase name contains one of the flash-score tokens.
    tokens = rules._FLASH_SCORE_NAME_TOKENS
    for e in b.events:
        nl = (e.name or "").lower()
        if any(tok in nl for tok in tokens):
            needed[e.source_id].add(e.row_idx)
    return needed


def _load_raw_kernel_details(root: Path, needed_rows: dict[str, set[int]] | None = None) -> dict:
    """Read original kernel_details rows referenced in source_index.json.

    Returns: ``{source_id: {row_idx: row_dict}}`` (row_idx is zero-based
    after the header, matching ``store.iter_csv_rows``).

    When ``needed_rows`` is given, only rows whose row index is in
    ``needed_rows[source_id]`` are materialised. The renderers never read
    rows outside that set (see ``_raw_rows_needed``), and skipping the rest
    is what keeps the report stage from holding full raw sources
    (100+ MB each) in memory just for the top-3 representative steps.
    """
    si_path = root / "source_index.json"
    if not si_path.exists():
        return {}
    si = json.loads(si_path.read_text(encoding="utf-8"))
    sources = si.get("sources", []) if isinstance(si, dict) else si
    by_source = {}
    for s in sources:
        if not isinstance(s, dict):
            continue
        kind = s.get("kind")
        if kind not in ("kernel_details_csv", "kernel_details_db"):
            continue
        path = Path(s["path"])
        if not path.exists():
            print(f"  WARN: source path missing: {path}", file=sys.stderr)
            continue
        wanted = needed_rows.get(s["source_id"]) if needed_rows is not None else None
        rows: dict[int, dict] = {}
        try:
            if kind == "kernel_details_db":
                # db-direct source: rebuild the same row dicts on demand so
                # operator cards keep their raw-row panel for db runs.
                try:
                    from ascend_profile import sources_db  # type: ignore
                except ImportError:  # pragma: no cover - script-mode fallback
                    import sources_db  # type: ignore[no-redef]

                for row_idx, row in sources_db.iter_kernel_events_from_db(path):
                    if wanted is not None and row_idx not in wanted:
                        continue
                    rows[row_idx] = row
            else:
                with path.open(encoding="utf-8") as f:
                    reader = csv.reader(f)
                    fieldnames = next(reader, None) or []
                    for row_idx, cells in enumerate(reader):
                        if wanted is not None and row_idx not in wanted:
                            continue
                        rows[row_idx] = dict(zip(fieldnames, cells))
        except Exception as exc:
            print(f"  WARN: failed reading {path}: {exc}", file=sys.stderr)
            continue
        by_source[s["source_id"]] = rows
        filter_note = " (filtered to render-needed rows)" if wanted is not None else ""
        print(f"  source {s['source_id'][:12]}… : {len(rows):,} rows{filter_note} ({path.name})", file=sys.stderr)
    return by_source


def _attach_raw_rows(events: list, raw_by_source: dict) -> int:
    hits = 0
    miss = 0
    for e in events:
        rows = raw_by_source.get(e.source_id)
        if not rows:
            miss += 1
            continue
        row = rows.get(e.row_idx)
        if row is None:
            miss += 1
            continue
        e.raw_row = row
        hits += 1
    print(f"  attached raw_row to {hits:,} events ({miss:,} miss)", file=sys.stderr)
    return hits


_COMM_NAME_HINTS = (
    "allreduce", "allgather", "reducescatter", "reduce_scatter",
    "broadcast", "alltoall", "all_to_all", "send", "recv",
    "dispatch", "combine",
)


def dedup_comm_aiv(events: list, iou_threshold: float = 0.9) -> int:
    """Mark AIV / mix_comm_aiv events that are dual-stream copies of an HCCL event.

    保守规则（两段都用）：
      A. op_type=mix_comm_aiv（dispatch/combine 等 fused kernel）必须与同 rank 的
         communication event 在时间上 IoU >= threshold → mark redundant。
      B. op_type=aiv 且 kernel 名命中通信关键词（allreduce/allgather/alltoall/...）
         且与同 rank 的 communication event 时间 IoU >= threshold → mark redundant。

    Rule B 防止"通信流上是 allreduce，计算流上是 aclnnAllReduce_xxx"被双重计入。
    AIV pipe 原始字段仍保留供分析。
    """
    if not events:
        return 0
    by_rank = defaultdict(list)
    for e in events:
        if e.op_type == "communication":
            by_rank[e.rank_id].append(e)
    starts_by_rank: dict[str, list] = {}
    prefix_max_end_by_rank: dict[str, list] = {}
    for rid in by_rank:
        by_rank[rid].sort(key=lambda x: x.start_us)
        cands = by_rank[rid]
        starts_by_rank[rid] = [c.start_us for c in cands]
        # prefix max of end_us: while scanning backwards we can stop as soon
        # as every remaining (earlier-started) candidate ends before the
        # query window -- they can never reach IoU >= threshold.
        prefix: list[float] = []
        running = 0.0
        for c in cands:
            running = c.end_us if c.end_us > running else running
            prefix.append(running)
        prefix_max_end_by_rank[rid] = prefix
    dedup = 0
    for e in events:
        if e.op_type == "mix_comm_aiv":
            pass  # rule A
        elif e.op_type == "aiv":
            nl = (e.name or "").lower()
            if not any(h in nl for h in _COMM_NAME_HINTS):
                continue
        else:
            continue
        cands = by_rank.get(e.rank_id, [])
        if not cands:
            continue
        # Candidates are sorted by start_us; only the prefix with
        # ``start_us <= e.end_us`` can overlap (equivalent to the old
        # ``break`` on the first candidate past e.end_us). Scan that prefix
        # backwards from the bisected boundary: same candidate set, same IoU
        # test, but we skip the long tail of candidates that both start and
        # end before the query window instead of walking them every time.
        starts = starts_by_rank[e.rank_id]
        prefix_max_end = prefix_max_end_by_rank[e.rank_id]
        i = bisect.bisect_right(starts, e.end_us) - 1
        while i >= 0 and prefix_max_end[i] >= e.start_us:
            c = cands[i]
            if c.end_us >= e.start_us:
                inter = max(0, min(c.end_us, e.end_us) - max(c.start_us, e.start_us))
                union = max(c.end_us, e.end_us) - min(c.start_us, e.start_us)
                iou = inter / union if union > 0 else 0.0
                if iou >= iou_threshold:
                    e.redundant = True
                    dedup += 1
                    break
            i -= 1
    return dedup


_RANK_EVENT_INDEX: dict[str, list] = {}
_RANK_EVENT_ROWS: dict[str, list] = {}


def _build_rank_event_index(events_by_row: list) -> None:
    """Build a per-rank, row-sorted event list + parallel row_idx array once."""
    global _RANK_EVENT_INDEX, _RANK_EVENT_ROWS
    _RANK_EVENT_INDEX = {}
    _RANK_EVENT_ROWS = {}
    bucket: dict[str, list] = defaultdict(list)
    for e in events_by_row:
        bucket[e.rank_id].append(e)
    for rid, lst in bucket.items():
        lst.sort(key=lambda x: x.row_idx)
        _RANK_EVENT_INDEX[rid] = lst
        _RANK_EVENT_ROWS[rid] = [e.row_idx for e in lst]


def _build_segments_index(segments: list, row_safe: bool = False) -> tuple[dict, dict, dict]:
    """Bucket segments by rank_id and sort each bucket by row_start.

    Returns ``(segs_by_rank, row_starts_by_rank, row_ends_by_rank)``.
    """
    buckets: dict[str, list] = defaultdict(list)
    for seg in segments:
        rid = seg.get("rank_id")
        if rid is None:
            continue
        buckets[rid].append(seg)
    by_rank: dict[str, list] = {}
    starts_by_rank: dict[str, list[int]] = {}
    ends_by_rank: dict[str, list[int]] = {}
    if row_safe:
        rs_key = lambda x: int(safe_float(x.get("row_start", 0)))
        re_key = lambda x: int(safe_float(x.get("row_end", 0)))
    else:
        rs_key = lambda x: int(x.get("row_start", 0))
        re_key = lambda x: int(x.get("row_end", 0))
    for rid, lst in buckets.items():
        lst.sort(key=rs_key)
        by_rank[rid] = lst
        starts_by_rank[rid] = [rs_key(x) for x in lst]
        ends_by_rank[rid] = [re_key(x) for x in lst]
    return by_rank, starts_by_rank, ends_by_rank


def _build_layer_seg_rank_index(b: "Bundle") -> None:
    """Build all per-rank segment indexes + id maps used by the renderers.

    Replaces multiple O(N) ``[ls for ls in b.layer_segments if ...]`` and
    ``next((s for s in b.step_segments if ...))`` scans that ran once per
    step in the L2/L3 renderers — the dominant cost on large multi-rank
    prefill traces.
    """
    (
        b._layer_segs_by_rank,
        b._layer_seg_row_starts_by_rank,
        b._layer_seg_row_ends_by_rank,
    ) = _build_segments_index(b.layer_segments)
    (
        b._block_segs_by_rank,
        b._block_seg_row_starts_by_rank,
        b._block_seg_row_ends_by_rank,
    ) = _build_segments_index(b.block_segments, row_safe=True)
    b._step_seg_by_id = {s.get("segment_id"): s for s in b.step_segments if s.get("segment_id")}
    b._step_anatomy_by_id = {a.get("segment_id"): a for a in b.step_anatomy if a.get("segment_id")}


def layer_segments_in_step(b: "Bundle", rank_id: str, row_start: int, row_end: int) -> list:
    """Return layer_segments fully contained within ``[row_start, row_end]``.

    O(log L + k) per call when the rank index is populated; O(L) fallback.
    A layer segment ``ls`` is "in the step" iff
    ``row_start <= ls.row_start`` AND ``ls.row_end <= row_end`` —
    matches the inclusive-on-both-ends convention used everywhere else.
    """
    lst = b._layer_segs_by_rank.get(rank_id)
    if lst is None:
        return [
            ls for ls in b.layer_segments
            if ls.get("rank_id") == rank_id
            and int(ls.get("row_start", 0)) >= row_start
            and int(ls.get("row_end", 0)) <= row_end
        ]
    starts = b._layer_seg_row_starts_by_rank.get(rank_id, [])
    ends = b._layer_seg_row_ends_by_rank.get(rank_id, [])
    lo = bisect.bisect_left(starts, row_start)
    hi = bisect.bisect_right(starts, row_end)
    out: list = []
    for idx in range(lo, hi):
        if ends[idx] <= row_end:
            out.append(lst[idx])
    return out


def block_segments_in_layer(b: "Bundle", rank_id: str, row_start: int, row_end: int) -> list:
    """Return block_segments fully contained within ``[row_start, row_end]``.

    Same bisect strategy as ``layer_segments_in_step``. Used by
    ``derive_layer_composition`` which previously did a full scan of
    ``b.block_segments`` for every layer in every step.
    """
    lst = b._block_segs_by_rank.get(rank_id)
    if lst is None:
        return [
            bs for bs in b.block_segments
            if bs.get("rank_id") == rank_id
            and int(safe_float(bs.get("row_start", 0))) >= row_start
            and int(safe_float(bs.get("row_end", 0))) <= row_end
        ]
    starts = b._block_seg_row_starts_by_rank.get(rank_id, [])
    ends = b._block_seg_row_ends_by_rank.get(rank_id, [])
    lo = bisect.bisect_left(starts, row_start)
    hi = bisect.bisect_right(starts, row_end)
    out: list = []
    for idx in range(lo, hi):
        if ends[idx] <= row_end:
            out.append(lst[idx])
    return out


def events_in_row_range(events_by_row: list, row_start: int, row_end: int, rank_id: str | None = None) -> list:
    """`events_by_row` must be pre-sorted by row_idx.

    Boundaries are **inclusive** on both ends: ``[row_start, row_end]``.
    This matches ``row_start`` / ``row_end`` everywhere else in the
    pipeline (``step_segments.json``, ``layer_segments.json``,
    ``block_segments.json``) which all use closed intervals — e.g. a
    block recorded as ``(row_start=106, row_end=121, event_count=16)``
    covers exactly the 16 rows ``106..121``.

    Using a half-open ``[row_start, row_end)`` here would silently drop
    the event sitting on the last row of every segment. That row is
    operationally significant: in vLLM-Ascend attention blocks the
    closing FIA / UnpadFlashAttention score kernel is precisely the
    last row, so a half-open query was making attention sub-type
    detection return ``attn`` (no flash_score category seen) instead
    of ``mha`` / ``gqa_or_mha`` / etc.

    IMPORTANT: row_idx is per-source (per-rank), not globally unique. When pulling events
    for a specific step/layer/block segment, ALWAYS pass rank_id to filter out events from
    other ranks that happen to share the same row_idx range. Otherwise the resulting events
    span multiple ranks' absolute timestamps and the timeline range explodes to global scale.

    O(log N + k) when rank_id is provided and the index is pre-built. Falls back to O(N).
    """
    if rank_id is not None and rank_id in _RANK_EVENT_INDEX:
        lst = _RANK_EVENT_INDEX[rank_id]
        rows = _RANK_EVENT_ROWS[rank_id]
        lo = bisect.bisect_left(rows, row_start)
        hi = bisect.bisect_right(rows, row_end)
        return lst[lo:hi]
    out = []
    for e in events_by_row:
        if e.row_idx < row_start:
            continue
        if e.row_idx > row_end:
            continue
        if rank_id is not None and e.rank_id != rank_id:
            continue
        out.append(e)
    return out


def load_bundle(root: Path, *, events=None) -> Bundle:
    b = Bundle(root=root)
    b.rank_summary = load_csv(root / "rank_summary.csv")
    b.step_summary = load_csv(root / "step_summary.csv")
    b.step_anatomy = load_csv(root / "step_anatomy.csv")
    b.step_class = load_csv(root / "step_class_summary.csv")
    b.layer_class = load_csv(root / "layer_class_summary.csv")
    b.block_class = load_csv(root / "block_class_summary.csv")
    b.operator_class = load_csv(root / "operator_class_summary.csv")
    b.hccl_class = load_csv(root / "hccl_class_summary.csv")
    b.hccl_op = load_csv(root / "hccl_op_summary.csv")
    findings_payload = load_json(root / "diagnosis_findings.json") or []
    if isinstance(findings_payload, dict):
        # The current schema writes `diagnosis_findings`; older drafts used
        # `findings`. Accept either to survive schema renames without losing
        # rows in the HTML view.
        findings = (
            findings_payload.get("diagnosis_findings")
            or findings_payload.get("findings")
            or findings_payload.get("claims")
            or []
        )
    else:
        findings = findings_payload
    b.findings = findings
    b.manifest = load_json(root / "manifest.json") or {}
    b.step_segments = _load_segments(root / "step_segments.json", "step_segments")
    b.layer_segments = _load_segments(root / "layer_segments.json", "layer_segments")
    b.block_segments = _load_segments(root / "block_segments.json", "block_segments")
    _build_layer_seg_rank_index(b)
    if events is None:
        print(f"loading events from normalized_event_index.csv ...", file=sys.stderr)
        b.events = _load_events(root / "normalized_event_index.csv")
    else:
        # In-process hand-off from the full-pipeline runner: adapt the
        # already-normalized events instead of re-parsing the CSV.
        b.events = [Event.from_normalized(event) for event in events]
    b.events.sort(key=lambda e: e.row_idx)
    _build_rank_event_index(b.events)
    print(f"  loaded {len(b.events)} events", file=sys.stderr)
    n_dedup = dedup_comm_aiv(b.events)
    print(f"  marked {n_dedup} comm-shadow events as redundant (mix_comm_aiv + AIV ops with comm-name keywords vs HCCL events, IoU >= 0.9)", file=sys.stderr)
    print(f"loading raw kernel_details.csv (per source) ...", file=sys.stderr)
    raw_by_source = _load_raw_kernel_details(root, _raw_rows_needed(b))
    _attach_raw_rows(b.events, raw_by_source)
    return b


def classify_workload(b: Bundle, rid: str):
    steps = [s for s in b.step_summary if s["rank_id"] == rid]
    if not steps:
        return ("b-mixed", "no data")
    dummy = sum(1 for s in steps if s.get("step_family") == "moe_or_dummy_workload")
    real = sum(1 for s in steps if s.get("step_family") == "attention_moe_workload")
    other = len(steps) - dummy - real
    if dummy >= 0.5 * len(steps) and real <= 0.2 * len(steps):
        return ("b-companion", f"companion · {dummy}/{len(steps)} dummy")
    if real > 0.8 * len(steps):
        return ("b-real", f"real · {real}/{len(steps)} attention+moe")
    return ("b-mixed", f"mixed · {dummy} dummy / {real} attention+moe / {other} other")


def pick_bound_stage(pipe: dict) -> str:
    """Return name of the dominant pipeline stage, ignoring aggregate aicore/aiv."""
    candidates = AIC_STAGES + AIV_STAGES
    best = None
    bestv = 0.0
    for k in candidates:
        v = safe_float(pipe.get(k))
        if v > bestv:
            best = k
            bestv = v
    return best or ""


def _stage_ratio_value(raw_row: dict, stage_time_field: str) -> float | None:
    """Get CANN-reported ratio for a pipeline stage (returns None if missing)."""
    ratio_field = STAGE_RATIO_FIELD.get(stage_time_field)
    if not ratio_field or not raw_row:
        return None
    raw_key = ratio_field if ratio_field in raw_row else None
    if raw_key is None:
        for k in raw_row.keys():
            if k.startswith(ratio_field):
                raw_key = k
                break
    if raw_key is None:
        return None
    v = raw_row.get(raw_key)
    try:
        return float(v)
    except Exception:
        return None


def _decide_bound_stage(e: Event) -> tuple[str, float | None, str]:
    """Pick decision stage using CANN ratio fields first (preferred), fall back to absolute time.

    Returns (stage_time_field_name, ratio_value_0to1, decision_basis_short)
    """
    # ratio-based first (only stages with ratio present)
    candidates = []
    for stage in AIC_STAGES + AIV_STAGES:
        if e.op_type == "aic" and stage in AIV_STAGES:
            continue
        if e.op_type == "aiv" and stage in AIC_STAGES:
            continue
        r = _stage_ratio_value(e.raw_row, stage)
        if r is not None:
            candidates.append((stage, r))
    if candidates:
        candidates.sort(key=lambda kv: -kv[1])
        s, r = candidates[0]
        return (s, r, "ratio")
    # fall back to absolute time
    s = pick_bound_stage(e.pipeline)
    return (s, None, "absolute_time")


_TIMELINE_COUNTER = [0]


# -----------------------------
# v7: SPA view renderers (L1 / L2 / L3)
# -----------------------------
