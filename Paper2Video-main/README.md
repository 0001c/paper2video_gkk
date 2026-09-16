# Paper2Video

<p align="right">
  <b>English</b> | <a href="./README-CN.md">简体中文</a>
</p>


<p align="center">
  <b>Paper2Video: Automatic Video Generation from Scientific Papers</b>
<br>
从学术论文自动生成演讲视频
</p>

<p align="center">
  <a href="https://zeyu-zhu.github.io/webpage/">Zeyu Zhu*</a>,
  <a href="https://qhlin.me/">Kevin Qinghong Lin*</a>,
  <a href="https://scholar.google.com/citations?user=h1-3lSoAAAAJ&hl=en">Mike Zheng Shou</a> <br>
  Show Lab, National University of Singapore
</p>


<p align="center">
  <a href="https://arxiv.org/abs/2510.05096">📄 Paper</a> &nbsp; | &nbsp;
  <a href="https://huggingface.co/papers/2510.05096">🤗 Daily Paper</a> &nbsp; | &nbsp;
  <a href="https://huggingface.co/datasets/ZaynZhu/Paper2Video">📊 Dataset</a> &nbsp; | &nbsp;
  <a href="https://showlab.github.io/Paper2Video/">🌐 Project Website</a> &nbsp; | &nbsp;
  <a href="https://x.com/KevinQHLin/status/1976105129146257542">💬 X (Twitter)</a>
</p>

- **Input:** a paper ➕ an image ➕ an audio
  
| Paper | Image | Audio |
|--------|--------|--------|
| <img src="https://github.com/showlab/Paper2Video/blob/page/assets/hinton/paper.png" width="180"/><br>[🔗 Paper link](https://arxiv.org/pdf/1509.01626) | <img src="https://github.com/showlab/Paper2Video/blob/page/assets/hinton/hinton_head.jpeg" width="180"/> <br>Hinton's photo| <img src="assets/sound.png" width="180"/><br>[🔗 Audio sample](https://github.com/showlab/Paper2Video/blob/page/assets/hinton/ref_audio_10.wav) |


- **Output:** a presentation video



https://github.com/user-attachments/assets/39221a9a-48cb-4e20-9d1c-080a5d8379c4




Check out more examples at [🌐 project page](https://showlab.github.io/Paper2Video/).

## 🔥 Update
**Any contributions are welcome!**
- [x] [2025.10.15] We update a new version without talking-head for fast generation!
- [x] [2025.10.11] Our work receives attention on [YC Hacker News](https://news.ycombinator.com/item?id=45553701).
- [x] [2025.10.9] Thanks AK for sharing our work on [Twitter](https://x.com/_akhaliq/status/1976099830004072849)!
- [x] [2025.10.9] Our work is reported by [Medium](https://medium.com/@dataism/how-ai-learned-to-make-scientific-videos-from-slides-to-a-talking-head-0d807e491b27).
- [x] [2025.10.8] Check out our demo video below!
- [x] [2025.10.7] We release the [arxiv paper](https://arxiv.org/abs/2510.05096).
- [x] [2025.10.6] We release the [code](https://github.com/showlab/Paper2Video) and [dataset](https://huggingface.co/datasets/ZaynZhu/Paper2Video).
- [x] [2025.9.28] Paper2Video has been accepted to the **Scaling Environments for Agents Workshop([SEA](https://sea-workshop.github.io/)) at NeurIPS 2025**.


https://github.com/user-attachments/assets/a655e3c7-9d76-4c48-b946-1068fdb6cdd9




---

### Table of Contents
- [🌟 Overview](#-overview)
- [🚀 Quick Start: PaperTalker](#-try-papertalker-for-your-paper-)
  - [1. Requirements](#1-requirements)
  - [2. Configure LLMs](#2-configure-llms)
  - [3. Inference](#3-inference)
- [📊 Evaluation: Paper2Video](#-evaluation-paper2video)
- [😼 Fun: Paper2Video for Paper2Video](#-fun-paper2video-for-paper2video)
- [🙏 Acknowledgements](#-acknowledgements)
- [📌 Citation](#-citation)

---

## 🌟 Overview
<p align="center">
  <img src="assets/teaser.png" alt="Overview" width="100%">
</p>

This work solves two core problems for academic presentations:

- **Left: How to create a presentation video from a paper?**  
  *PaperTalker* — an agent that integrates **slides**, **subtitling**, **cursor grounding**, **speech synthesis**, and **talking-head video rendering**.

- **Right: How to evaluate a presentation video?**  
  *Paper2Video* — a benchmark with well-designed metrics to evaluate presentation quality.


---

## 🚀 Try PaperTalker for your Paper!
<p align="center">
  <img src="assets/method.png" alt="Approach" width="100%">
</p>

### 1. Requirements
Prepare the environment:
```bash
cd src
conda create -n p2v python=3.10
conda activate p2v
pip install -r requirements.txt
conda install -c conda-forge tectonic
```
**[Optional] [Skip](#2-configure-llms) this part if you do not need a human presenter.**

Download the dependent code and follow the instructions in **[Hallo2](https://github.com/fudan-generative-vision/hallo2)** to download the model weight.
```bash
git clone https://github.com/fudan-generative-vision/hallo2.git
```
You need to **prepare the environment separately for talking-head generation** to potential avoide package conflicts, please refer to  <a href="git clone https://github.com/fudan-generative-vision/hallo2.git">Hallo2</a>. After installing, use `which python` to get the python environment path.
```bash
cd hallo2
conda create -n hallo python=3.10
conda activate hallo
pip install -r requirements.txt
```

### 2. Configure LLMs
Export your **API credentials**:
```bash
export GEMINI_API_KEY="your_gemini_key_here"
export OPENAI_API_KEY="your_openai_key_here"
```
The best practice is to use **GPT4.1** or **Gemini2.5-Pro** for both LLM and VLMs. We also support locally deployed open-source model(e.g., Qwen), details please referring to <a href="https://github.com/Paper2Poster/Paper2Poster.git">Paper2Poster</a>.

### 3. Inference
The script `pipeline.py` provides an automated pipeline for generating academic presentation videos. It takes **LaTeX paper sources** together with **reference image/audio** as input, and goes through multiple sub-modules (Slides → Subtitles → Speech → Cursor → Talking Head) to produce a complete presentation video. ⚡ The minimum recommended GPU for running this pipeline is **NVIDIA A6000** with 48G.

#### Example Usage
Run the following command to launch a fast generation (**without talking-head generation**):
```bash
python pipeline_light.py \
    --model_name_t gpt-4.1 \
    --model_name_v gpt-4.1 \
    --result_dir /path/to/output \
    --paper_latex_root /path/to/latex_proj \
    --ref_img /path/to/ref_img.png \
    --ref_audio /path/to/ref_audio.wav \
    --gpu_list [0,1,2,3,4,5,6,7]
```

Run the following command to launch a full generation (**with talking-head generation**):

```bash
python pipeline.py \
    --model_name_t gpt-4.1 \
    --model_name_v gpt-4.1 \
    --model_name_talking hallo2 \
    --result_dir /path/to/output \
    --paper_latex_root /path/to/latex_proj \
    --ref_img /path/to/ref_img.png \
    --ref_audio /path/to/ref_audio.wav \
    --talking_head_env /path/to/hallo2_env \
    --gpu_list [0,1,2,3,4,5,6,7]
```

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--model_name_t` | `str` | `gpt-4.1` | LLM |
| `--model_name_v` | `str` | `gpt-4.1` | VLM |
| `--model_name_talking` | `str` | `hallo2` | Talking Head model. Currently only **hallo2** is supported |
| `--result_dir` | `str` | `/path/to/output` | Output directory (slides, subtitles, videos, etc.) |
| `--paper_latex_root` | `str` | `/path/to/latex_proj` | Root directory of the LaTeX paper project |
| `--ref_img` | `str` | `/path/to/ref_img.png` | Reference image (must be **square** portrait) |
| `--ref_audio` | `str` | `/path/to/ref_audio.wav` | Reference audio (recommended: ~10s) |
| `--ref_text` | `str` | `None` | Optional reference text (for style guidance for subtitles) |
| `--beamer_templete_prompt` | `str` | `None` | Optional reference text (for style guidance for slides) |
| `--gpu_list` | `list[int]` | `""` | GPU list for parallel execution (used in **cursor generation** and **Talking Head rendering**) |
| `--if_tree_search` | `bool` | `True` | Whether to enable tree search for slide layout refinement |
| `--stage` | `str` | `"[0]"` | Pipeline stages to run (e.g., `[0]` full pipeline, `[1,2,3]` partial stages) |
| `--talking_head_env` | `str` | `/path/to/hallo2_env` | python environment path for talking-head generation |
---

## 📊 Evaluation: Paper2Video
<p align="center">
  <img src="assets/metrics.png" alt="Metrics" width="100%">
</p>

Unlike natural video generation, academic presentation videos serve a highly specialized role: they are not merely about visual fidelity but about **communicating scholarship**. This makes it difficult to directly apply conventional metrics from video synthesis(e.g., FVD, IS, or CLIP-based similarity). Instead, their value lies in how well they **disseminate research** and **amplify scholarly visibility**.From this perspective, we argue that a high-quality academic presentation video should be judged along two complementary dimensions:
#### For the Audience
- The video is expected to **faithfully convey the paper’s core ideas**.  
- It should remain **accessible to diverse audiences**.  

#### For the Author
- The video should **foreground the authors’ intellectual contribution and identity**.  
- It should **enhance the work’s visibility and impact**.  

To capture these goals, we introduce evaluation metrics specifically designed for academic presentation videos: Meta Similarity, PresentArena, PresentQuiz, IP Memory.

### Run Eval
- Prepare the environment:
```bash
cd src/evaluation
conda create -n p2v_e python=3.10
conda activate p2v_e
pip install -r requirements.txt
```
- For MetaSimilarity and PresentArena:
```bash
python MetaSim_audio.py --r /path/to/result_dir --g /path/to/gt_dir --s /path/to/save_dir
python MetaSim_content.py --r /path/to/result_dir --g /path/to/gt_dir --s /path/to/save_dir
```
```bash
python PresentArena.py --r /path/to/result_dir --g /path/to/gt_dir --s /path/to/save_dir
```
- For **PresentQuiz**, first generate questions from paper and eval using Gemini:
```bash
cd PresentQuiz
python create_paper_questions.py ----paper_folder /path/to/data
python PresentQuiz.py --r /path/to/result_dir --g /path/to/gt_dir --s /path/to/save_dir
```

- For **IP Memory**, first generate question pairs from generated videos and eval using Gemini:
```bash
cd IPMemory
python construct.py
python ip_qa.py
```
See the codes for more details!

👉 Paper2Video Benchmark is available at:
[HuggingFace](https://huggingface.co/datasets/ZaynZhu/Paper2Video)

---

## 😼 Fun: Paper2Video for Paper2Video
Check out **How Paper2Video for Paper2Video**:

https://github.com/user-attachments/assets/ff58f4d8-8376-4e12-b967-711118adf3c4

## 🙏 Acknowledgements

* The souces of the presentation videos are SlideLive and YouTuBe.
* We thank all the authors who spend a great effort to create presentation videos!
* We thank [CAMEL](https://github.com/camel-ai/camel) for open-source well-organized multi-agent framework codebase.
* We thank the authors of [Hallo2](https://github.com/fudan-generative-vision/hallo2.git) and [Paper2Poster](https://github.com/Paper2Poster/Paper2Poster.git) for their open-sourced codes.
* We thank [Wei Jia](https://github.com/weeadd) for his effort in collecting the data and implementing the baselines. We also thank all the participants involved in the human studies.
* We thank all the **Show Lab @ NUS** members for support!



---

## 📌 Citation


If you find our work useful, please cite:

```bibtex
@misc{paper2video,
      title={Paper2Video: Automatic Video Generation from Scientific Papers}, 
      author={Zeyu Zhu and Kevin Qinghong Lin and Mike Zheng Shou},
      year={2025},
      eprint={2510.05096},
      archivePrefix={arXiv},
      primaryClass={cs.CV},
      url={https://arxiv.org/abs/2510.05096}, 
}
```
[![Star History](https://api.star-history.com/svg?repos=showlab/Paper2Video&type=Date)](https://star-history.com/#showlab/Paper2Video&Date)
