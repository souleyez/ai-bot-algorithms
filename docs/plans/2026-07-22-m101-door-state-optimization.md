# M101 Fixed-Door State Detection Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task.

**Goal:** 将 61672 的 2 通道小门监控从通用画面变化检测改造成固定机位的开门/关门状态识别，消除阳光、树影和行人导致的误报，同时保持约 10 秒检测、8 秒复核、连续 3 次确认后上报。

**Architecture:** 保留 m101 通用巡检能力，新增可按设备配置启用的 `door_state` 检测模式。门状态检测只分析门洞右侧窄 ROI，使用亮度归一化后的结构特征和开/关参考模板做三态判定（`open`、`closed`、`unknown`），再由独立状态机确认状态转换；不再用全局直方图差异直接判断开门，也不在告警后覆盖闭门参考图。

**Tech Stack:** Python 3、OpenCV、NumPy、pytest、现有 systemd `m101-scene-change.service`、现有 smart-gw MQTT/抓拍链路。

---

## 现状与问题

- 当前设备配置：通道 2、ROI `[0.505, 0.2, 0.57, 0.31]`、间隔 10 秒、复核 8 秒、连续 3 次。
- 2026-07-22 的三张告警图（09:21、10:36、13:20）均为闭门状态。
- 误报主要来自日照强度变化、树影移动和人员经过。
- 当前 `change_score` 同时依赖灰度、直方图、块差异和边缘差异；即使门结构未变，强光仍可持续推高分数。
- `update_baseline_after_scene_alarm=true` 会把误报画面写成新基准，不适合固定门状态。
- `same_view_motion` 是通用画面位移抑制逻辑，不应决定局部门扇是否打开。

## 目标判定规则

1. `closed`：黑色栅栏门扇结构填满两根黑柱之间的门洞，右侧没有明显通透缝隙。
2. `open`：门扇向左打开，门洞右侧出现持续通透区域，且结构更接近开门参考模板。
3. `unknown`：人员遮挡、严重反光、坏帧、参考不足或开关状态得分接近；未知状态不告警。
4. 连续 3 次 `open` 才从稳定 `closed` 转为 `open` 并上报一次。
5. 连续 3 次 `closed` 才从稳定 `open` 恢复为 `closed`；默认只记录状态，不上报告警。
6. 单次 `open`、`unknown` 或一闪而过的变化不能形成告警。

## 验收标准

- 今天三张误报图全部判为 `closed` 或 `unknown`，不能判为 `open`。
- 至少收集 50 张不同时段闭门图，离线回放零开门告警。
- 至少收集 10 张真实开门图，持续开门时召回率达到 100%。
- 开门保持后，首次上报告警不超过 60 秒。
- 行人经过、树影移动、亮度骤变和短暂遮挡不能形成连续开门告警。
- 部署后 m101 仍只有一个进程，CPU 和内存不高于当前方案的 1.2 倍。
- 不重启 `nnmgd`，不修改模型列表，不绑定或改写设备通道配置。

### Task 1: 建立私有回放样本集

**Files:**
- Create: `tools/m101_door/build_replay_manifest.py`
- Create: `services/m101-scene-change/tests/fixtures/door/README.md`
- Modify: `.gitignore`
- Runtime only: `.runtime/m101-door-replay/61672-ch2/`

**Steps:**

1. 从设备只读收集今天三张误报原图、当前闭门图和最近时段候选图。
2. 由操作员安排一次真实开门，连续抓取至少 10 张无 UI 叠加的 RTSP 原帧。
3. 将图片标记为 `closed`、`open`、`unknown`，生成只含相对路径、时间和标签的 manifest。
4. 在 `.gitignore` 中明确忽略私有图片、manifest 实例和生成报告；仓库只保留格式说明。
5. 运行 manifest 校验，确保文件存在、无重复哈希、三类标签合法。

**Verification:**

```powershell
python tools/m101_door/build_replay_manifest.py --input .runtime/m101-door-replay/61672-ch2 --check
```

Expected: 输出样本数、各标签数量和重复项为 0，不复制或上传客户图片。

### Task 2: 实现光照不敏感的门状态特征

**Files:**
- Create: `services/m101-scene-change/door_state_detector.py`
- Create: `services/m101-scene-change/tests/test_door_state_detector.py`

**Steps:**

1. 先写失败测试：全局增亮、减暗和轻微树影不能显著改变闭门模板得分。
2. 实现 ROI 裁剪、灰度归一化和 CLAHE；禁止使用绝对平均亮度作为开门证据。
3. 生成 Sobel/Canny 结构图和垂直边缘投影，比较右侧门缝区域的栅栏结构保留程度。
4. 分别计算与闭门模板、开门模板的结构相似度，并输出 `open_score`、`closed_score` 和差值 margin。
5. 当两个模板得分接近、ROI 被大面积遮挡或帧质量差时返回 `unknown`。
6. 对今天三张误报图执行测试，预期为 `closed` 或 `unknown`。

**Core API:**

```python
def classify_door_state(
    frame,
    closed_reference,
    open_reference,
    roi,
    config,
) -> dict:
    """Return state, open_score, closed_score, margin, quality and evidence."""
```

**Verification:**

```powershell
pytest services/m101-scene-change/tests/test_door_state_detector.py -v
```

Expected: 光照、树影、行人遮挡和真实开门测试全部通过。

### Task 3: 实现三态时序状态机

**Files:**
- Create: `services/m101-scene-change/door_state_machine.py`
- Create: `services/m101-scene-change/tests/test_door_state_machine.py`

**Steps:**

1. 先写失败测试：`open, open, open` 只产生一次开门转换。
2. 写测试：`open, closed, open` 不能告警，计数应清零。
3. 写测试：`unknown` 不制造状态转换，也不把稳定闭门误改为开门。
4. 实现开门和关门分别计数的迟滞状态机。
5. 将稳定状态、候选计数和最后转换时间持久化到 `state.json` 的独立 `door_state` 字段。

**Verification:**

```powershell
pytest services/m101-scene-change/tests/test_door_state_machine.py -v
```

Expected: 所有状态转换和复位规则通过。

### Task 4: 以配置开关接入 m101

**Files:**
- Modify: `services/m101-scene-change/m101_scene_change_service.py`
- Modify: `services/m101-scene-change/config.example.json`
- Modify: `services/m101-scene-change/README.md`
- Create: `services/m101-scene-change/tests/test_service_detector_routing.py`

**Steps:**

1. 新增配置 `channel_detectors`，默认不存在时继续走原有通用画面变化逻辑。
2. 通道配置为 `type=door_state` 时加载闭门和开门参考图，并调用新检测器。
3. 保留 10 秒间隔、8 秒复核和 3 次确认；`unknown` 不写抓拍。
4. 固定门模式下禁止 `update_baseline_after_scene_alarm` 更新门参考图。
5. 开门确认后继续复用现有 smart-gw 抓拍和消息格式，不新增外部 URL。
6. 写路由回归测试，证明其他通道和通用 m101 行为不变。

**Example device-only config:**

```json
{
  "channel_detectors": {
    "2": {
      "type": "door_state",
      "roi": [0.505, 0.2, 0.57, 0.31],
      "closed_reference": "/oem/smart-gw/m101_scene_change/door_refs/ch2_closed.jpg",
      "open_reference": "/oem/smart-gw/m101_scene_change/door_refs/ch2_open.jpg",
      "open_margin": 0.15,
      "close_margin": 0.10,
      "open_confirm_count": 3,
      "close_confirm_count": 3
    }
  }
}
```

**Verification:**

```powershell
pytest services/m101-scene-change/tests -v
python -m py_compile services/m101-scene-change/m101_scene_change_service.py
```

Expected: 新测试通过，旧通用模式可加载，配置示例 JSON 合法。

### Task 5: 离线回放与阈值标定

**Files:**
- Create: `tools/m101_door/replay_door_detector.py`
- Runtime only: `.runtime/m101-door-replay/reports/`

**Steps:**

1. 按时间顺序回放私有样本，不直接在设备上试错。
2. 输出每帧状态、两个模板得分、margin、质量标记和状态机计数。
3. 以“闭门零误报”为优先调节 `open_margin`，再确认真实开门召回。
4. 固化本设备阈值到设备配置候选文件，不写入通用默认值。
5. 生成 JSON 报告，并人工复核所有错误样本。

**Verification:**

```powershell
python tools/m101_door/replay_door_detector.py `
  --manifest .runtime/m101-door-replay/61672-ch2/manifest.json `
  --report .runtime/m101-door-replay/reports/61672-ch2.json
```

Expected: 满足本计划验收标准后才允许部署。

### Task 6: 安全部署到 61672

**Files:**
- Create: `tools/bastion/deploy_61672_m101_door_state.py`
- Runtime only: `.runtime/m101-61672-door-state-config.json`

**Steps:**

1. 预检 m101 active、单进程、当前脚本哈希、配置和参考图目录。
2. 备份服务脚本、配置、`state.json` 和原闭门基准图。
3. 上传新模块、脚本、设备专用配置及两张参考图到临时路径。
4. 在设备执行 `py_compile` 和 JSON 校验后原子替换。
5. 仅执行 `systemctl restart m101-scene-change.service`。
6. 验证新 PID、单进程、10 秒周期、8 秒复核、3 次确认及 smart-gw 输出链路。
7. 明确核对 `nnmgd` PID、模型目录和通道绑定未被修改。

**Rollback:**

恢复本次备份的服务脚本、配置、状态和参考图，然后只重启 `m101-scene-change.service`。

### Task 7: 现场灰度验收

**Steps:**

1. 先观察至少 4 小时闭门状态，覆盖阳光和树影变化。
2. 人工开门并保持 60 秒，确认一次且仅一次开门抓拍。
3. 关门并保持 60 秒，确认状态恢复为 `closed`。
4. 安排人员在门前经过，确认返回 `unknown` 或 `closed`，不告警。
5. 连续观察 24 小时；出现误报立即回滚，不在线反复调阈值。
6. 验收通过后更新 m101 README 和 61672 部署记录，但私有图像不进入 Git。

## 推荐实施顺序

优先完成 Task 1 的真实开门/闭门样本采集，再实现 Task 2-5 并离线验收。没有真实开门原帧时不得直接部署模板分类器；今天三张误报图只能证明闭门误报特征，不能替代开门参考样本。
