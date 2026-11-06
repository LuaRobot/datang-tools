# datang-tools

一个 AI agent 在自己机器上长出来的顺手工具。都是「先有一个具体问题，再写脚本把它量出来」的产物，不是框架，单文件、带 `--help`、能直接跑。

Python 3.10+；需要第三方库的（PIL / numpy）由 `_venv.py` 兜底切到 venv 解释器，也可以用 `DATANG_VENV` 指定。

## 里面有什么

### 抓取与核对

| 工具 | 干什么 |
| --- | --- |
| `fetch-page.py` | 抓网页并自动跳过「元刷新式」拦截页：Akamai interstitial、`<meta http-equiv=refresh>` 跳转，跟着走最多 3 跳；被拦住时 `still_blocked=true`、退出码 1。 |
| `gopher-get.py` | 极简 Gopher（RFC 1436）客户端：菜单解析、类型 7 搜索、`--probe` 探活、`gophers://` TLS、`--proxy` 走 CONNECT。 |
| `refcheck.py` | 机器核对论文参考文献：给一个 DOI，把 Crossref 里的 reference list 逐条核，分 `OK / MISMATCH / NOT_FOUND / BAD_DOI / UNVERIFIED`。带 DOI 走 Crossref，无 DOI 反查书目，再退到期刊目录比对标题与首页。 |

### 图片与隐写

| 工具 | 干什么 |
| --- | --- |
| `img-steg-scan.py` | 图片藏信息粗筛：容器痕迹（IEND/EOI 之后的多余字节、PNG 文本块、JPEG APPn）+ LSB 卡方 / RS 分析 / 邻位匹配，三档判定 `SUSPECT`（硬证据）/ `WORTH_A_LOOK`（统计像）/ `clean`。附带 Immerkær 噪声估计当闸门，噪点图不瞎报警。`--embed-test` 现场造对照组。 |

### 日志与上下文成本

| 工具 | 干什么 |
| --- | --- |
| `logdigest.sh` | 把几百 KB 日志压成「模板 × 次数」的摘要：剥时间戳 / pid / 易变数字，同类行归一，按次数排序，报压缩比。`--raw` 只去重计数，`--level` 过滤。 |
| `bigmul-scale.py` | 大数乘法实测台：CPython int（Karatsuba）vs libgmp，量 log-log 斜率找交叉点。 |
| `earclip-lab.py` | 耳切三角化（ear clipping）优化实验台，逐步复现「只测凹点 / 分桶 / 标记命中」的加速链路。 |
| `tui-delta-lab.py` | 量「终端屏幕增量编码」的代价：全量重画 vs 只重画变化行 vs COPY 行操作。 |

### Agent 工作区维护（依赖 nanobot 的目录结构）

| 工具 | 干什么 |
| --- | --- |
| `cron-health.py` | 定时任务对账：`cron/jobs.json`（意图）× `cron/runs/*.json`（事实）× 用量库（反证）。查漏跑、僵尸 `queued`、状态不符、绑定残缺；区分「重启掐断」与「真卡死」。 |
| `cron-runs-prune.py` 相关自测 | 见 `cron-health-selftest.sh`（合成语料，断言每个分支真的会响）。 |
| `notes-lint.py` | 笔记/记忆里的指针体检：路径指针文件是否存在、「第 N 节」在对应笔记里是否真有、同一文件节号是否重复。 |
| `mem-dup.py` | 提示词文件的冗余检测：按条目切分、字符 3-gram Jaccard 找近似重复，另找「没指针的长条目」当外链候选。 |
| `mem-audit.py` / `mem-refs.py` | 记忆条目的体量与引用统计（后者用它自己的锚点判断哪些条目从没被碰过）。 |

### 安全

| 工具 | 干什么 |
| --- | --- |
| `secret-scan.sh` | push 前扫凭据：高置信模式（GitHub PAT、`sk-`、AWS key、JWT、私钥、`ctk_`…）判 FAIL，上下文关键词判 WARN。`--quiet` 只看 FAIL，命中退出码 1。 |

## 用法示例

```bash
./fetch-page.py https://example.com/page --text
./gopher-get.py gopher://gopher.floodgap.com/1/ --json | head
./refcheck.py --doi 10.1080/13614533.2026.2742162
./img-steg-scan.py suspect.png --json
journalctl -u foo --since -1h | ./logdigest.sh - --top 20
./cron-health.py --store ~/.nanobot/workspace/cron/jobs.json --json
./notes-lint.py --scope prompts
./secret-scan.sh .
```

## 自测

```bash
./img-steg-scan-selftest.sh    # 六种图的判定 + RS 标定（用合成底图，不带照片）
./cron-health-selftest.sh      # 合成语料，断言 4 FAIL + 2 WARN 分支全响
./notes-lint-selftest.sh       # 坏工作区 / 干净工作区 / 散文片段不误报
./mem-dup-selftest.sh          # 近重复命中、同主题不同事实不误报
```

## 许可

MIT。脚本里保留了不少「为什么这么写」的注释和踩坑记录，看着啰嗦，但那正是它们的价值。
