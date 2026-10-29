# data/ — third-party benchmark data

The files here are **not** covered by the repository's MIT license. They are copies
of public benchmarks, kept only so the local proxy evaluations can be reproduced
(`tools/gallery_proxy.py`, `tools/e2e_proxy.py`, `tools/atm_proxy.py`).
They are not AML evaluation data; no AML private data is stored in this repository.

| Path | Source | License | Citation |
|---|---|---|---|
| `mem-gallery/dialog/` | [Ethan-Bei/Mem-Gallery](https://huggingface.co/datasets/Ethan-Bei/Mem-Gallery) · code [YuanchenBei/Mem-Gallery](https://github.com/YuanchenBei/Mem-Gallery) | MIT | Yuanchen Bei, Tianxin Wei, Xuying Ning, et al. *Mem-Gallery: Benchmarking Multimodal Long-Term Conversational Memory for MLLM Agents.* arXiv:2601.03515 |
| `atm-bench/`, `raw_memory/email/`, `atm_doc_ids.json` | [Jingbiao/ATM-Bench](https://huggingface.co/datasets/Jingbiao/ATM-Bench) · code [JingbiaoMei/ATM-Bench](https://github.com/JingbiaoMei/ATM-Bench) | **CC BY-NC 4.0** (non-commercial) | Jingbiao Mei, Jinghong Chen, Guangyu Yang, et al. *According to Me: Long-Term Personalized Referential Memory QA.* arXiv:2603.01990 |

- `atm_doc_ids.json` is derived from ATM-Bench (document id list) and inherits CC BY-NC 4.0.
- Images (`mem-gallery/image/`) and derived vectors/caches (`*.npy`, `atm_docs.json`, `.cache/`)
  are git-ignored; download images from the original dataset if needed.
- Do not use the ATM-Bench files for commercial purposes. If you redistribute them,
  keep the attribution above and link to the CC BY-NC 4.0 license
  (https://creativecommons.org/licenses/by-nc/4.0/).
